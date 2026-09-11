"""Master-data tools: ledgers, parties, and resolving what a human called something."""

from __future__ import annotations

import difflib
from datetime import date
from decimal import Decimal

from tallyagent_core import idempotency
from tallyagent_core.models import Ledger, Party, Voucher, VoucherLine, VoucherType
from tallyagent_core.validation import gstin as gstin_mod
from tallyagent_tally.xml import builders
from tallyagent_tools.base import PendingAction, ToolContext, ToolResult


async def list_ledgers(
    ctx: ToolContext, group: str = "", refresh: bool = False
) -> ToolResult:
    """List ledger masters, optionally filtered to one group."""
    masters = await ctx.masters(refresh=refresh)
    ledgers = [
        {"name": lg.name, "group": lg.parent, "gstin": lg.gstin or ""}
        for lg in masters.ledgers
        if not group or lg.parent == group
    ]
    where = f" under {group}" if group else ""
    return ToolResult(message=f"{len(ledgers)} ledger(s){where}.", data=ledgers)


async def resolve_ledger_alias(ctx: ToolContext, name: str) -> ToolResult:
    """Map what someone typed to a real ledger.

    Returns an exact match, a learned alias, or *suggestions* - never a silent
    substitution. Choosing between near-matches is a human's call; getting it
    wrong books revenue against the wrong customer.
    """
    masters = await ctx.masters()
    names = masters.ledger_names

    if name in names:
        return ToolResult(
            message=f"{name!r} is an existing ledger.",
            data={"resolved": name, "how": "exact"},
        )

    alias = ctx.ledger_aliases.get(name) or ctx.ledger_aliases.get(name.lower())
    if alias and alias in names:
        return ToolResult(
            message=f"{name!r} is a known alias for {alias!r}.",
            data={"resolved": alias, "how": "alias"},
        )

    suggestions = difflib.get_close_matches(name, sorted(names), n=5, cutoff=0.5)
    return ToolResult(
        message=(
            f"No ledger named {name!r}. Closest: {', '.join(suggestions)}."
            if suggestions
            else f"No ledger named {name!r} and nothing close to it."
        ),
        data={"resolved": None, "how": "unresolved", "suggestions": suggestions},
    )


async def _submit_master(
    ctx: ToolContext,
    action_type: str,
    summary: str,
    element: object,
    payload: dict[str, object],
) -> ToolResult:
    """Masters go through the same approval gate as vouchers.

    They carry no ValidationReport (the rules are voucher-shaped), so an
    unapproved master is simply queued; policy can auto-approve it, and the
    executor calls the right backend method from ``payload["kind"]``.
    """
    ctx.live.require_write_scope(ctx.company.name)
    # A master has no voucher, so the idempotency key is derived from a
    # synthetic one-line voucher standing in for "create this master".
    stand_in = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=ctx.company.period.start if ctx.company.period else date.today(),
        reference=f"{action_type}:{payload.get('name', '')}",
        lines=[VoucherLine(ledger_name=str(payload.get("name", "")), amount=Decimal("0"))],
    )
    pending = PendingAction(
        action_type=action_type,
        company=ctx.company.name,
        summary=summary,
        idempotency_key=idempotency.make_key(ctx.company.name, stand_in),
        raw_xml=builders.to_string(element),  # type: ignore[arg-type]
        source=ctx.source,
        payload=payload,
    )

    if not ctx.policy.requires_approval(action_type, Decimal("0")):
        result = await _execute_master(ctx, pending)
        return ToolResult(
            message=(
                f"Created automatically under policy: {summary}"
                if result.ok
                else f"Tally rejected it: {'; '.join(result.errors)}"
            ),
            write=result,
        )

    if ctx.enqueue is None:
        return ToolResult(message=f"Awaiting approval: {summary}", pending=pending)
    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued for approval as {ticket}: {summary}",
        data={"ticket": ticket},
        pending=pending,
    )


async def _execute_master(ctx: ToolContext, pending: PendingAction):  # type: ignore[no-untyped-def]
    """Perform a queued master creation. Shared with the approvals executor."""
    payload = pending.payload
    if payload.get("kind") == "party":
        party = Party.model_validate(payload["party"])
        return await ctx.backend.create_party(  # type: ignore[attr-defined]
            party, pending.idempotency_key, ctx.company.name
        )
    ledger = Ledger.model_validate(payload["ledger"])
    return await ctx.backend.create_ledger(
        ledger, pending.idempotency_key, ctx.company.name
    )


async def create_ledger(
    ctx: ToolContext,
    name: str,
    group: str,
    opening_balance: Decimal | str = "0",
    gstin: str = "",
    state: str = "",
) -> ToolResult:
    """Create a ledger master under an existing group."""
    masters = await ctx.masters()
    if name in masters.ledger_names:
        return ToolResult(message=f"Ledger {name!r} already exists; nothing to do.")
    if gstin and not gstin_mod.is_valid(gstin):
        return ToolResult(message=f"Refused: {gstin!r} is not a valid GSTIN.")

    ledger = Ledger(
        name=name,
        parent=group,
        opening_balance=Decimal(str(opening_balance)),
        gstin=gstin or None,
        state=state or None,
    )
    return await _submit_master(
        ctx,
        "create_ledger",
        f"Create ledger {name!r} under {group!r}",
        builders.build_ledger_element(ledger),
        {"kind": "ledger", "name": name, "ledger": ledger.model_dump(mode="json")},
    )


async def create_party(
    ctx: ToolContext,
    name: str,
    is_customer: bool = True,
    gstin: str = "",
    state_code: str = "",
    credit_period_days: int = 0,
) -> ToolResult:
    """Create a customer or supplier."""
    masters = await ctx.masters()
    if name in masters.ledger_names:
        return ToolResult(message=f"Party {name!r} already exists; nothing to do.")
    if gstin and not gstin_mod.is_valid(gstin):
        return ToolResult(message=f"Refused: {gstin!r} is not a valid GSTIN.")

    party = Party(
        name=name,
        is_customer=is_customer,
        gstin=gstin or None,
        state_code=state_code or (gstin_mod.state_code(gstin) if gstin else None),
        credit_period_days=credit_period_days,
    )
    role = "customer" if is_customer else "supplier"
    return await _submit_master(
        ctx,
        "create_party",
        f"Create {role} {name!r}",
        builders.build_party_element(party),
        {"kind": "party", "name": name, "party": party.model_dump(mode="json")},
    )
