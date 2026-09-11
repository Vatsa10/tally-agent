"""Taking a fresh TallyPrime from an empty Select Company screen to a populated,
usable company.

The order of attempts for company creation is set by what live Tally actually
does, not by preference. On TallyPrime 1.1.7.1 every ``Import Data`` request
resolves a current company *first*, so with none loaded a company-create import
comes back ``<LINEERROR>Could not find Company ''</LINEERROR>``. The XML attempt
is still made first - a later Tally version may well support it, and the
attempt is cheap and harmless - and the result is recorded in the audit log so
we know which path this installation needed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from lxml import etree

from tallyagent_core import idempotency
from tallyagent_core.models import Ledger, Party, Voucher, VoucherLine, VoucherType
from tallyagent_tally.xml import builders, parsers
from tallyagent_tally.xml.quirks import snap_to_edu_date, to_tally_date
from tallyagent_tools.base import PendingAction, ToolContext, ToolResult

log = logging.getLogger(__name__)

#: How long to wait for a newly created company to appear in List of Companies.
COMPANY_APPEAR_SECONDS = 30

#: What the Tier 3 fallback is asked to do when XML creation is refused.
CREATE_COMPANY_TASK = (
    "Create a new company in TallyPrime. From the Select Company screen press "
    "Alt+F3 (Company Info) or choose Create Company, then fill in: Name "
    "{name}, State {state}, Country India, Financial year beginning {fy_start}, "
    "Books beginning {fy_start}"
    "{gstin_hint}. Accept the screen to save. Do not change anything else."
)


@dataclass(slots=True)
class BootstrapResult:
    ok: bool = False
    company: str = ""
    attempt: str = ""  # xml | tier3 | existing | none
    used_tier3: bool = False
    xml_errors: list[str] = field(default_factory=list)
    loaded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "company": self.company,
            "attempt": self.attempt,
            "used_tier3": self.used_tier3,
            "xml_errors": self.xml_errors,
            "loaded": self.loaded,
        }


# --- company creation -------------------------------------------------------


def build_company_element(
    name: str,
    state: str,
    fy_start: date,
    gstin: str = "",
    address: str = "",
    books_from: date | None = None,
) -> etree._Element:
    """The ``<COMPANY>`` master, built with lxml like every other request."""
    element = etree.Element("COMPANY", attrib={"NAME": name, "ACTION": "Create"})
    etree.SubElement(element, "NAME").text = name
    etree.SubElement(element, "BASICCOMPANYFORMALNAME").text = name
    etree.SubElement(element, "MAILINGNAME").text = name
    etree.SubElement(element, "STARTINGFROM").text = to_tally_date(fy_start)
    etree.SubElement(element, "BOOKSFROM").text = to_tally_date(books_from or fy_start)
    etree.SubElement(element, "STATENAME").text = state
    etree.SubElement(element, "COUNTRYNAME").text = "India"
    etree.SubElement(element, "ISSECURITYON").text = "No"
    if address:
        address_list = etree.SubElement(element, "ADDRESS.LIST")
        etree.SubElement(address_list, "ADDRESS").text = address
    if gstin:
        etree.SubElement(element, "GSTREGNUMBER").text = gstin
        etree.SubElement(element, "ISGSTONSALVALUE").text = "No"
    return element


async def _create_via_xml(
    ctx: ToolContext, element: etree._Element
) -> tuple[bool, list[str]]:
    """Attempt A. Returns ``(created, errors)``; never raises on a LINEERROR."""
    client = ctx.backend.client  # type: ignore[attr-defined]
    payload = builders.build_import([element], "All Masters", company="")
    try:
        raw = await client.post(payload)
    except Exception as exc:  # noqa: BLE001 - an attempt, not a commitment
        return False, [f"{type(exc).__name__}: {exc}"]
    result = parsers.parse_import_result(raw)
    return bool(result.created), result.messages


async def _wait_for_company(ctx: ToolContext, name: str) -> bool:
    client = ctx.backend.client  # type: ignore[attr-defined]
    deadline = asyncio.get_event_loop().time() + COMPANY_APPEAR_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        try:
            if name in await client.list_companies():
                return True
        except Exception:  # noqa: BLE001 - Tally restarts during creation
            pass
        await asyncio.sleep(2)
    return False


async def bootstrap_company(
    ctx: ToolContext,
    name: str = "TA-Demo Traders",
    state: str = "Gujarat",
    gstin: str = "",
    fy_start: date | None = None,
    address: str = "",
    books_from: date | None = None,
    approve: Any = None,
) -> ToolResult:
    """Create a company and make it the active one.

    The name must be inside the live-mode write scope: bootstrapping a company
    we would then be forbidden to write to is never what anyone wants.
    """
    ctx.live.require_write_scope(name)
    fy_start = fy_start or date(2026, 4, 1)
    outcome = BootstrapResult(company=name)

    client = ctx.backend.client  # type: ignore[attr-defined]
    try:
        existing = await client.list_companies()
    except Exception as exc:  # noqa: BLE001
        return ToolResult(
            message=f"Cannot reach Tally to create a company: {exc}",
            data=outcome.as_dict(),
        )

    if name in existing:
        outcome.ok = outcome.loaded = True
        outcome.attempt = "existing"
        _activate(ctx, name)
        return ToolResult(
            message=f"Company {name!r} already exists and is now the active company.",
            data=outcome.as_dict(),
        )

    element = build_company_element(name, state, fy_start, gstin, address, books_from)

    created, errors = await _create_via_xml(ctx, element)
    outcome.xml_errors = errors
    if created and await _wait_for_company(ctx, name):
        outcome.ok = outcome.loaded = True
        outcome.attempt = "xml"
        _activate(ctx, name)
        return ToolResult(
            message=(
                f"Created company {name!r} over XML and made it active. "
                "This Tally supports API company creation."
            ),
            data=outcome.as_dict(),
        )

    log.info("XML company creation refused (%s); trying the UI", errors)

    runner = getattr(ctx, "fallback", None)
    if runner is None:
        return ToolResult(
            message=(
                f"This TallyPrime will not create a company over XML "
                f"({'; '.join(errors) or 'no rows written'}), and the Tier 3 "
                "fallback is not enabled, so it cannot be created from here. "
                "Either enable Tier 3 (/tier3 on) or create "
                f"{name!r} once in Tally by hand; everything after that is API."
            ),
            data=outcome.as_dict(),
        )

    outcome.used_tier3 = True
    outcome.attempt = "tier3"
    if approve is not None:
        runner.approve = approve
    session = await runner.run(
        CREATE_COMPANY_TASK.format(
            name=name,
            state=state,
            fy_start=fy_start.strftime("%d-%m-%Y"),
            gstin_hint=f", GSTIN {gstin}" if gstin else "",
        )
    )
    if not session.completed:
        return ToolResult(
            message=f"Could not create {name!r} through the UI: {session.stopped_reason}",
            data=outcome.as_dict(),
        )

    outcome.loaded = await _wait_for_company(ctx, name)
    outcome.ok = outcome.loaded
    if outcome.ok:
        _activate(ctx, name)
    return ToolResult(
        message=(
            f"Created company {name!r} through the Tally UI and verified it over "
            "XML. This Tally does not support API company creation."
            if outcome.ok
            else f"Drove the Create Company screen but {name!r} is not visible over "
            "XML. Check the Tally window."
        ),
        data=outcome.as_dict(),
    )


def _activate(ctx: ToolContext, name: str) -> None:
    """Point the tool context at the company and drop the stale masters cache."""
    ctx.company = ctx.company.model_copy(update={"name": name})
    ctx.backend.client.config.company = name  # type: ignore[attr-defined]
    ctx._masters_cache = None


# --- chart of accounts ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerSpec:
    name: str
    group: str
    opening: str = "0"


#: The ledgers the voucher tools already assume exist (D-028). Creating them is
#: how a fresh company becomes usable without the agent inventing masters.
TRADING_GST = (
    LedgerSpec("Sales - GST 18%", "Sales Accounts"),
    LedgerSpec("Purchase - GST 18%", "Purchase Accounts"),
    LedgerSpec("Output CGST", "Duties & Taxes"),
    LedgerSpec("Output SGST", "Duties & Taxes"),
    LedgerSpec("Output IGST", "Duties & Taxes"),
    LedgerSpec("Input CGST", "Duties & Taxes"),
    LedgerSpec("Input SGST", "Duties & Taxes"),
    LedgerSpec("Input IGST", "Duties & Taxes"),
    LedgerSpec("Round Off", "Indirect Expenses"),
    LedgerSpec("Bank - HDFC 1234", "Bank Accounts", "250000.00"),
    LedgerSpec("Cash", "Cash-in-Hand", "15000.00"),
)

SERVICES_GST = (
    LedgerSpec("Professional Fees", "Sales Accounts"),
    LedgerSpec("Output CGST", "Duties & Taxes"),
    LedgerSpec("Output SGST", "Duties & Taxes"),
    LedgerSpec("Output IGST", "Duties & Taxes"),
    LedgerSpec("Input CGST", "Duties & Taxes"),
    LedgerSpec("Input SGST", "Duties & Taxes"),
    LedgerSpec("Input IGST", "Duties & Taxes"),
    LedgerSpec("Round Off", "Indirect Expenses"),
    LedgerSpec("Bank - HDFC 1234", "Bank Accounts", "250000.00"),
    LedgerSpec("Cash", "Cash-in-Hand", "15000.00"),
)

#: The contra for whatever opening balances actually get created. Computed at
#: seed time rather than hardcoded: Tally ships some ledgers (Cash, Profit &
#: Loss A/c) already, and a ledger that already exists does not get our opening,
#: so a fixed capital figure leaves the books out by the difference.
CAPITAL_LEDGER = "Capital Account"
CAPITAL_GROUP = "Capital Account"

PROFILES = {"trading_gst": TRADING_GST, "services_gst": SERVICES_GST}

#: Demo parties. GSTINs are the checksum-valid ones from tests/fixtures.
DEMO_PARTIES = (
    Party(
        name="Acme Industries",
        gstin="27AAPFU0939F1ZV",
        state_code="27",
        is_customer=True,
        credit_period_days=30,
    ),
    Party(
        name="Bharat Supplies",
        gstin="29AAGCB7383J1Z4",
        state_code="29",
        is_customer=False,
        credit_period_days=15,
    ),
)


async def seed_chart_of_accounts(
    ctx: ToolContext, profile: str = "trading_gst"
) -> ToolResult:
    """Create the standard ledgers and demo parties as **one** approval.

    One batched action with one diff, not twenty tickets: an approver asked to
    click through twenty near-identical master creations stops reading them,
    which is worse than showing them one list.
    """
    ctx.live.require_write_scope(ctx.company.name)
    specs = PROFILES.get(profile)
    if specs is None:
        return ToolResult(
            message=f"unknown profile {profile!r}; try {', '.join(sorted(PROFILES))}"
        )

    masters_snapshot = await ctx.masters(refresh=True)
    existing = masters_snapshot.ledger_names

    ledgers = [
        Ledger(name=s.name, parent=s.group, opening_balance=Decimal(s.opening))
        for s in specs
        if s.name not in existing
    ]
    parties = [p for p in DEMO_PARTIES if p.name not in existing]

    # Balance whatever openings we are about to create, and only those.
    opening_total = sum((lg.opening_balance for lg in ledgers), Decimal("0"))
    if opening_total and CAPITAL_LEDGER not in existing:
        ledgers.append(
            Ledger(
                name=CAPITAL_LEDGER,
                parent=CAPITAL_GROUP,
                opening_balance=-opening_total,
            )
        )

    if not ledgers and not parties:
        return ToolResult(
            message=f"The {profile} chart of accounts is already in place; nothing to do."
        )

    elements = [builders.build_ledger_element(lg) for lg in ledgers]
    elements += [builders.build_party_element(p) for p in parties]
    combined = etree.Element("MASTERS")
    for element in elements:
        combined.append(element)

    summary = (
        f"Create {len(ledgers)} ledger(s) and {len(parties)} part(y/ies) "
        f"for the {profile} chart of accounts"
    )
    listing = "\n".join(
        [f"  ledger  {lg.name}  ({lg.parent})" for lg in ledgers]
        + [
            f"  party   {p.name}  ({'customer' if p.is_customer else 'supplier'}, "
            f"{p.gstin})"
            for p in parties
        ]
    )

    stand_in = _stand_in_voucher(ctx, f"seed:{profile}")
    pending = PendingAction(
        action_type="seed_chart_of_accounts",
        company=ctx.company.name,
        summary=summary,
        idempotency_key=idempotency.make_key(ctx.company.name, stand_in),
        raw_xml=builders.to_string(combined),
        source=ctx.source,
        payload={
            "kind": "masters_batch",
            "ledgers": [lg.model_dump(mode="json") for lg in ledgers],
            "parties": [p.model_dump(mode="json") for p in parties],
        },
    )

    if ctx.enqueue is None:
        return ToolResult(message=f"{summary}\n{listing}", pending=pending)
    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued as {ticket}: {summary}\n{listing}",
        data={"ticket": ticket, "ledgers": len(ledgers), "parties": len(parties)},
        pending=pending,
    )


def _stand_in_voucher(ctx: ToolContext, reference: str) -> Voucher:
    """A synthetic voucher so a master batch can have an idempotency key."""
    when = ctx.company.period.start if ctx.company.period else date(2026, 4, 1)
    return Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=when,
        reference=reference,
        lines=[VoucherLine(ledger_name=reference, amount=Decimal("0"))],
    )


# --- demo transactions ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoTxn:
    tool: str
    kwargs: dict[str, Any]


def demo_transactions(count: int, edu: bool, fy_start: date) -> list[DemoTxn]:
    """A small, balanced set of transactions on dates Tally will accept.

    Dates are EDU-legal by construction rather than snapped afterwards: seeding
    should not emit a warning on every voucher.
    """
    month = fy_start.month
    year = fy_start.year

    def when(offset_months: int, day: int) -> date:
        m = month + offset_months
        y = year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        chosen = date(y, m, min(day, 28) if not edu else day)
        return chosen

    plan = [
        DemoTxn(
            "create_sales_voucher",
            {
                "party_name": "Acme Industries",
                "taxable_value": "50000.00",
                "gst_rate": "18",
                "voucher_date": when(0, 1),
                "reference": "INV-001",
                "narration": "Sale of goods",
            },
        ),
        DemoTxn(
            "create_purchase_voucher",
            {
                "party_name": "Bharat Supplies",
                "taxable_value": "20000.00",
                "gst_rate": "18",
                "voucher_date": when(0, 2),
                "reference": "BS/2026/41",
                "narration": "Packaging material",
            },
        ),
        DemoTxn(
            "create_receipt",
            {
                "party_name": "Acme Industries",
                "amount": "59000.00",
                "voucher_date": when(1, 1),
                "narration": "NEFT ACME INDS",
            },
        ),
        DemoTxn(
            "create_payment",
            {
                "party_name": "Bharat Supplies",
                "amount": "23600.00",
                "voucher_date": when(1, 2),
                "narration": "RTGS BHARAT SUPPLIES",
            },
        ),
        DemoTxn(
            "create_sales_voucher",
            {
                "party_name": "Acme Industries",
                "taxable_value": "35000.00",
                "gst_rate": "18",
                "voucher_date": when(1, 31),
                "reference": "INV-002",
                "narration": "Sale of goods",
            },
        ),
    ]
    return plan[: max(0, count)]


async def seed_demo_transactions(
    ctx: ToolContext, count: int = 4
) -> ToolResult:
    """Post demo vouchers through the normal tools and the normal approval queue.

    Deliberately *not* a shortcut straight to the backend: seeding is the first
    real exercise of the voucher path against this Tally, and it should fail the
    same way a user's first invoice would.
    """
    ctx.live.require_write_scope(ctx.company.name)
    from tallyagent_tools import registry

    fy_start = ctx.company.period.start if ctx.company.period else date(2026, 4, 1)
    plan = demo_transactions(count, ctx.live.edu, fy_start)

    tickets: list[str] = []
    problems: list[str] = []
    for txn in plan:
        kwargs = dict(txn.kwargs)
        if ctx.live.edu:
            snapped, warning = snap_to_edu_date(kwargs["voucher_date"])
            kwargs["voucher_date"] = snapped
            if warning:
                problems.append(warning)
        result = await registry.call(ctx, txn.tool, kwargs)
        if isinstance(result.data, dict) and result.data.get("ticket"):
            tickets.append(str(result.data["ticket"]))
        elif not result.ok:
            problems.append(f"{txn.tool}: {result.message}")

    message = f"Queued {len(tickets)} demo voucher(s) for approval: {', '.join(tickets)}"
    if problems:
        message += "\n" + "\n".join(f"  ! {p}" for p in problems)
    return ToolResult(message=message, data={"tickets": tickets, "problems": problems})
