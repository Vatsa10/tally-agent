"""TallyPrime implementation of AccountingBackend.

This is where the idempotency guarantee lives: a write is recorded as in-flight
*before* the envelope leaves, and a replay of a completed key returns the
original result without touching Tally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from tallyagent_backends.accounting_backend import (
    LedgerEntry,
    Masters,
    OutstandingBill,
    WriteResult,
)
from tallyagent_core import idempotency
from tallyagent_core.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from tallyagent_core.models import Ledger, Party, Voucher
from tallyagent_core.models.voucher import PAISA
from tallyagent_tally.client import TallyClient
from tallyagent_tally.xml import builders, parsers
from tallyagent_tally.xml.quirks import from_tally_date, to_tally_date

log = logging.getLogger(__name__)

#: Tally stores a state *name*; GST works in *codes*. Both directions are
#: needed: reading masters maps name -> code, writing them maps code -> name.
STATE_CODES = {
    "Maharashtra": "27",
    "Karnataka": "29",
    "Delhi": "07",
    "Gujarat": "24",
    "Tamil Nadu": "33",
    "Uttar Pradesh": "09",
    "West Bengal": "19",
    "Telangana": "36",
    "Haryana": "06",
    "Rajasthan": "08",
    "Gujarat ": "24",
}
STATE_CODES.pop("Gujarat ", None)

STATE_NAMES = {code: name for name, code in STATE_CODES.items()}


@dataclass(slots=True)
class _OpenBill:
    """One bill reference and what is still open on it."""

    reference: str
    amount: Decimal
    date: date | None = None


def _party_state_code(gstin: str | None, state_name: str | None) -> str | None:
    """A party's GST state code.

    The GSTIN's first two digits are the authoritative place of supply for a
    registered party, so they win. The state name is the fallback for an
    unregistered party, and Tally does not always return one.
    """
    if gstin and len(gstin.strip()) >= 2 and gstin.strip()[:2].isdigit():
        return gstin.strip()[:2]
    if state_name:
        return STATE_CODES.get(state_name)
    return None


class TallyBackend:
    """Satisfies ``AccountingBackend``."""

    name = "tally"

    def __init__(
        self,
        client: TallyClient,
        store: IdempotencyStore | None = None,
    ) -> None:
        self.client = client
        self.store: IdempotencyStore = store or InMemoryIdempotencyStore()

    # --- reads --------------------------------------------------------------

    async def probe(self) -> dict[str, str]:
        return await self.client.probe()

    async def list_companies(self) -> list[str]:
        return await self.client.list_companies()

    #: Real Tally's plain "List of Ledgers" collection returns *names only* -
    #: no parent, no GSTIN, no opening balance. Fields have to be asked for
    #: explicitly with a TDL FETCH. Verified against TallyPrime 1.1.7.1; the
    #: fake server reproduces both shapes.
    LEDGER_FETCH = (
        "NAME,PARENT,OPENINGBALANCE,PARTYGSTIN,LEDSTATENAME,"
        "GSTREGISTRATIONTYPE,MASTERID"
    )
    LEDGER_TDL = (
        '<COLLECTION NAME="TALedgers" ISMODIFY="No">'
        "<TYPE>Ledger</TYPE>"
        f"<FETCH>{LEDGER_FETCH}</FETCH>"
        "</COLLECTION>"
    )

    async def get_masters(self, company: str | None = None) -> Masters:
        rows = await self.client.export_collection(
            "TALedgers", "LEDGER", company=company, tdl=self.LEDGER_TDL
        )
        ledgers: list[Ledger] = []
        parties: list[Party] = []
        groups: set[str] = set()
        for row in rows:
            name = str(row.get("NAME") or row.get("@NAME") or "").strip()
            if not name:
                continue
            parent = str(row.get("PARENT") or "").strip()
            state = str(row.get("LEDSTATENAME") or "").strip() or None
            ledger = Ledger(
                name=name,
                parent=parent,
                opening_balance=parsers.to_decimal(row.get("OPENINGBALANCE")),
                gstin=str(row.get("PARTYGSTIN") or "").strip() or None,
                state=state,
                master_id=str(row.get("MASTERID") or "").strip() or None,
            )
            ledgers.append(ledger)
            groups.add(parent)
            if ledger.is_party:
                parties.append(
                    Party(
                        name=name,
                        ledger_name=name,
                        gstin=ledger.gstin,
                        state_code=_party_state_code(ledger.gstin, state),
                        is_customer=parent == "Sundry Debtors",
                        master_id=ledger.master_id,
                    )
                )
        return Masters(ledgers=ledgers, parties=parties, groups=sorted(groups))

    #: Voucher fields to fetch for day-book-shaped reports. Real Tally returns
    #: nothing at all from the plain "Day Book" report export, so these come
    #: from a TDL collection like the ledgers do.
    VOUCHER_TDL = (
        '<COLLECTION NAME="TAVouchers" ISMODIFY="No">'
        "<TYPE>Voucher</TYPE>"
        "<FETCH>DATE,VOUCHERTYPENAME,VOUCHERNUMBER,PARTYLEDGERNAME,REFERENCE,"
        "NARRATION,MASTERID,ALLLEDGERENTRIES.LIST</FETCH>"
        "</COLLECTION>"
    )

    #: Reports that are really voucher listings.
    VOUCHER_REPORTS = frozenset({"Day Book", "Voucher Register", "Ledger Vouchers"})

    async def get_vouchers(
        self,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        ledger_name: str = "",
    ) -> list[dict[str, object]]:
        """Voucher rows, one per ledger entry, in *our* sign convention.

        Amounts arrive negated from Tally; they are flipped here, at the same
        boundary where every other sign conversion happens.
        """
        extra = {"SVLEDGERNAME": ledger_name} if ledger_name else None
        payload = builders.build_export_collection(
            collection_name="TAVouchers",
            company=self.client.company_or_default(company),
            username=self.client.config.username,
            password=self.client.config.password,
            from_date=to_tally_date(from_date) if from_date else "",
            to_date=to_tally_date(to_date) if to_date else "",
            extra_vars=extra,
            tdl=self.VOUCHER_TDL,
        )
        rows = parsers.parse_voucher_rows(await self.client.post(payload))
        for row in rows:
            if row.get("AMOUNT") not in (None, ""):
                row["AMOUNT"] = format(-parsers.to_decimal(row["AMOUNT"]), "f")

        # SVFROMDATE, SVTODATE and SVLEDGERNAME are honoured by *reports*, not by
        # a TDL collection: Tally returns the whole book regardless. Filtering
        # has to happen here, or "the day book for June" quietly means "every
        # voucher ever", and so does every figure derived from it.
        if from_date or to_date:
            kept = []
            for row in rows:
                when = from_tally_date(str(row.get("DATE") or ""))
                if when is None:
                    continue
                if from_date and when < from_date:
                    continue
                if to_date and when > to_date:
                    continue
                kept.append(row)
            rows = kept
        if ledger_name:
            rows = [row for row in rows if row.get("LEDGERNAME") == ledger_name]
        return rows

    async def get_report(
        self,
        report_name: str,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        if report_name in self.VOUCHER_REPORTS:
            return await self.get_vouchers(
                company=company,
                from_date=from_date,
                to_date=to_date,
                ledger_name=(extra_vars or {}).get("SVLEDGERNAME", ""),
            )
        element_tag = {
            "Trial Balance": "LEDGER",
            "List of Ledgers": "LEDGER",
            "Bills Receivable": "BILLS",
            "Bills Payable": "BILLS",
        }.get(report_name, "VOUCHER")
        return await self.client.export_report(
            report_name,
            element_tag,
            company=company,
            from_date=from_date,
            to_date=to_date,
            extra_vars=extra_vars,
        )

    async def ledger_balances(
        self,
        company: str | None = None,
        as_on: date | None = None,
    ) -> dict[str, Decimal]:
        """Closing balance per ledger, derived rather than asked for.

        TallyPrime 1.1.7.1 returns nothing from the Trial Balance report export,
        and fetching ``CLOSINGBALANCE`` on a Ledger collection hangs the process
        outright (see docs/DECISIONS.md D-110). Both of those are dead ends, so
        the balance is computed the way a ledger actually works: opening balance
        plus every posting. Same arithmetic Tally does, from data we can get
        reliably, and it agrees with Tally's own Trial Balance screen.
        """
        masters = await self.get_masters(company)
        balances: dict[str, Decimal] = {
            ledger.name: ledger.opening_balance for ledger in masters.ledgers
        }
        for row in await self.get_vouchers(company=company, to_date=as_on):
            name = str(row.get("LEDGERNAME") or "")
            if not name:
                continue
            balances[name] = balances.get(name, Decimal("0")) + parsers.to_decimal(
                row.get("AMOUNT")
            )
        return balances

    async def get_outstanding(
        self,
        receivable: bool = True,
        company: str | None = None,
        as_on: date | None = None,
    ) -> list[OutstandingBill]:
        """Outstanding bills, derived from bill-wise allocations.

        Like the trial balance, the Bills Receivable/Payable report exports come
        back empty, so this is built from the vouchers: every ``New Ref``
        allocation opens a bill and every ``Agst Ref`` settles part of one. What
        is left un-netted is outstanding. Entries with no bill reference fall
        back to a single "(on account)" bill per party, which is exactly how
        Tally shows them.
        """
        masters = await self.get_masters(company)
        parties = {
            party.name: party
            for party in masters.parties
            if party.is_customer is receivable
        }
        if not parties:
            return []

        openings = {
            ledger.name: ledger.opening_balance
            for ledger in masters.ledgers
            if ledger.name in parties
        }

        rows = await self.get_vouchers(company=company, to_date=as_on)

        # The party's ledger balance is the authority. Whatever the bill-wise
        # breakdown says, the outstanding total for a party MUST equal it -
        # otherwise the receivables report and the trial balance disagree, and
        # a CA has two numbers and no way to choose.
        balances: dict[str, Decimal] = dict(openings)
        opened: dict[str, list[_OpenBill]] = {name: [] for name in parties}
        allocated: dict[str, Decimal] = dict.fromkeys(parties, Decimal("0"))
        # An on-account amount still has to be ageable, so remember the first
        # date the party was transacted with. Without it every on-account
        # balance reports as "not due" for ever, which is the opposite of what
        # a receivables report is for.
        first_seen: dict[str, date] = {}

        for row in rows:
            ledger = str(row.get("LEDGERNAME") or "")
            if ledger not in parties:
                continue
            when = from_tally_date(str(row.get("DATE") or ""))
            if when and (ledger not in first_seen or when < first_seen[ledger]):
                first_seen[ledger] = when
            balances[ledger] = balances.get(ledger, Decimal("0")) + parsers.to_decimal(
                row.get("AMOUNT")
            )
            for bill in row.get("BILLS") or []:
                if not isinstance(bill, dict) or not bill.get("name"):
                    continue
                # Allocation amounts arrive in Tally's convention too.
                amount = -parsers.to_decimal(bill.get("amount"))
                allocated[ledger] += amount
                existing = next(
                    (b for b in opened[ledger] if b.reference == bill["name"]), None
                )
                if existing is None:
                    opened[ledger].append(
                        _OpenBill(reference=str(bill["name"]), amount=amount, date=when)
                    )
                else:
                    existing.amount += amount
                    if when and (existing.date is None or when < existing.date):
                        existing.date = when

        today = as_on or date.today()
        bills: list[OutstandingBill] = []

        for party_name, party in parties.items():
            balance = balances.get(party_name, Decimal("0")).quantize(PAISA)
            if balance == 0:
                continue

            open_bills = [b for b in opened[party_name] if b.amount.quantize(PAISA) != 0]
            open_bills.sort(key=lambda b: (b.date or date.max, b.reference))

            # Anything not booked against a named bill - an "On Account"
            # receipt, or an opening balance - is settled against the oldest
            # bills first, the way a payment on account actually behaves.
            unallocated = balance - allocated.get(party_name, Decimal("0"))
            if openings.get(party_name):
                unallocated -= Decimal("0")  # the opening is already in balance
            for bill in open_bills:
                if unallocated == 0:
                    break
                if (unallocated > 0) == (bill.amount > 0):
                    continue  # same direction; it is not a settlement
                applied = min(abs(unallocated), abs(bill.amount))
                bill.amount -= applied if bill.amount > 0 else -applied
                unallocated += applied if unallocated < 0 else -applied

            remaining = [b for b in open_bills if b.amount.quantize(PAISA) != 0]
            if unallocated.quantize(PAISA) != 0:
                remaining.append(
                    _OpenBill(
                        reference="(on account)",
                        amount=unallocated,
                        date=first_seen.get(party_name),
                    )
                )

            for bill in remaining:
                # A receivable is a debit balance, a payable a credit one;
                # either way the figure shown is positive.
                if receivable and bill.amount < 0:
                    continue
                if not receivable and bill.amount > 0:
                    continue
                due = (
                    bill.date + timedelta(days=party.credit_period_days)
                    if bill.date and party.credit_period_days
                    else bill.date
                )
                bills.append(
                    OutstandingBill(
                        party_name=party_name,
                        bill_reference=bill.reference,
                        bill_date=bill.date,
                        amount=abs(bill.amount).quantize(PAISA),
                        due_date=due,
                        overdue_days=(today - due).days if due else 0,
                    )
                )
        return bills

    async def find_vouchers(
        self,
        company: str | None = None,
        reference: str = "",
        voucher_number: str = "",
        voucher_type: str = "",
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, str]]:
        """Find posted vouchers and, crucially, their Tally ids.

        Amending needs an id Tally can match. ``REMOTEID`` is a GUID and is the
        safest: ``MASTERID`` is only unique within a company, and the id
        returned by an import is a sequence number that is not either of them.
        """
        seen: dict[str, dict[str, str]] = {}
        for row in await self.get_vouchers(
            company=company, from_date=from_date, to_date=to_date
        ):
            number = str(row.get("VOUCHERNUMBER") or "")
            kind = str(row.get("VOUCHERTYPENAME") or "")
            ref = str(row.get("REFERENCE") or "")
            if reference and ref != reference:
                continue
            if voucher_number and number != voucher_number:
                continue
            if voucher_type and kind.lower() != voucher_type.lower():
                continue
            key = f"{kind}|{number}|{ref}"
            seen.setdefault(
                key,
                {
                    "voucher_type": kind,
                    "voucher_number": number,
                    "reference": ref,
                    "date": str(row.get("DATE") or ""),
                    "master_id": str(row.get("MASTERID") or ""),
                    "remote_id": str(row.get("REMOTEID") or ""),
                    "narration": str(row.get("NARRATION") or ""),
                },
            )
        return list(seen.values())

    async def get_bank_ledger(
        self,
        ledger_name: str,
        from_date: date,
        to_date: date,
        company: str | None = None,
    ) -> list[LedgerEntry]:
        rows = await self.get_vouchers(
            company=company,
            from_date=from_date,
            to_date=to_date,
            ledger_name=ledger_name,
        )
        return [
            LedgerEntry(
                date=from_tally_date(str(row.get("DATE") or "")),
                voucher_type=str(row.get("VOUCHERTYPENAME") or ""),
                voucher_number=str(row.get("VOUCHERNUMBER") or ""),
                ledger_name=str(row.get("LEDGERNAME") or ledger_name),
                particulars=str(row.get("PARTYLEDGERNAME") or ""),
                amount=parsers.to_decimal(row.get("AMOUNT")),
                narration=str(row.get("NARRATION") or ""),
                master_id=str(row.get("MASTERID") or ""),
            )
            for row in rows
        ]

    # --- writes -------------------------------------------------------------

    async def _import_once(
        self,
        elements: list[object],
        request_type: str,
        idempotency_key: str,
        company: str | None,
        expect_altered: bool = False,
    ) -> WriteResult:
        """Idempotency gate + import + result recording, shared by every write."""
        target = self.client.company_or_default(company)
        decision = idempotency.check(self.store, idempotency_key, target)
        if not decision.should_execute:
            existing = decision.existing
            log.info("idempotent no-op for %s: %s", idempotency_key, decision.reason)
            return WriteResult(
                ok=existing is not None and existing.status == "succeeded",
                voucher_number=(existing.result.get("voucher_number", "") if existing else ""),
                master_id=(existing.result.get("master_id", "") if existing else ""),
                idempotency_key=idempotency_key,
                replayed=True,
                errors=[] if (existing and existing.status == "succeeded") else [decision.reason],
            )

        try:
            result, raw = await self.client.import_elements(
                elements,  # type: ignore[arg-type]
                request_type,
                company=company,
            )
        except Exception as exc:
            idempotency.record_failure(self.store, idempotency_key, str(exc))
            raise

        problems = list(result.messages)
        if not result.errors and not (result.created or result.altered):
            problems.append("Tally wrote nothing and gave no reason")
        if expect_altered and result.created and not result.altered:
            # The id did not match anything, so Tally treated an amendment as a
            # new voucher. Silently duplicating a voucher is far worse than
            # refusing, so this is a failure even though Tally said CREATED 1.
            problems.append(
                "the amendment did not match an existing voucher: Tally created a "
                f"new one instead (created={result.created}, altered={result.altered}). "
                "The duplicate must be removed in Tally."
            )
        if problems:
            idempotency.record_failure(self.store, idempotency_key, "; ".join(problems))
            return WriteResult(
                ok=False,
                idempotency_key=idempotency_key,
                errors=problems,
                raw_request=raw,
            )

        payload = {
            "voucher_number": result.last_voucher_id,
            "master_id": result.last_master_id or result.last_voucher_id,
        }
        idempotency.record_success(self.store, idempotency_key, payload)
        return WriteResult(
            ok=True,
            voucher_number=result.last_voucher_id,
            master_id=payload["master_id"],
            idempotency_key=idempotency_key,
            raw_request=raw,
        )

    async def create_voucher(
        self,
        voucher: Voucher,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult:
        element = builders.build_voucher_element(voucher, action="Create")
        return await self._import_once([element], "Vouchers", idempotency_key, company)

    async def alter_voucher(
        self,
        voucher: Voucher,
        master_id: str,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult:
        if not self.client.config.supports_voucher_alter:
            return WriteResult(
                ok=False,
                idempotency_key=idempotency_key,
                errors=[
                    "this TallyPrime does not support amending a voucher over XML. "
                    "Its import is create-only: an Alter is not matched against the "
                    "existing voucher, it silently creates a duplicate (verified "
                    "against REMOTEID, VCHKEY, GUID and MASTERID on 1.1.7.1). "
                    "Amend it in Tally, or set [tally] supports_voucher_alter = true "
                    "if your version handles it."
                ],
            )
        if not str(master_id).strip() or str(master_id).strip() == "0":
            # Sending a blank or zero id makes Tally create a duplicate rather
            # than amend anything. Refuse before it leaves.
            return WriteResult(
                ok=False,
                idempotency_key=idempotency_key,
                errors=[
                    "cannot amend a voucher without its Tally id. Look it up with "
                    "find_voucher first - an empty id makes Tally create a "
                    "duplicate instead of amending."
                ],
            )
        amended = voucher.model_copy(update={"master_id": master_id})
        element = builders.build_voucher_element(
            amended, action="Alter", remote_id=master_id
        )
        return await self._import_once(
            [element], "Vouchers", idempotency_key, company, expect_altered=True
        )

    async def create_ledger(
        self,
        ledger: Ledger,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult:
        element = builders.build_ledger_element(ledger)
        return await self._import_once(
            [element], "All Masters", idempotency_key, company
        )

    async def create_party(
        self,
        party: Party,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult:
        element = builders.build_party_element(party)
        return await self._import_once(
            [element], "All Masters", idempotency_key, company
        )

    # --- convenience --------------------------------------------------------

    async def trial_balance(self, company: str | None = None) -> dict[str, Decimal]:
        return await self.ledger_balances(company=company)
