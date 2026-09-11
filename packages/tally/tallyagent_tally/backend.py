"""TallyPrime implementation of AccountingBackend.

This is where the idempotency guarantee lives: a write is recorded as in-flight
*before* the envelope leaves, and a replay of a completed key returns the
original result without touching Tally.
"""

from __future__ import annotations

import logging
from datetime import date
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
from tallyagent_tally.client import TallyClient
from tallyagent_tally.xml import builders, parsers
from tallyagent_tally.xml.quirks import from_tally_date

log = logging.getLogger(__name__)

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
}


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

    async def get_masters(self, company: str | None = None) -> Masters:
        rows = await self.client.export_collection(
            "List of Ledgers", "LEDGER", company=company
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
                        state_code=STATE_CODES.get(state or ""),
                        is_customer=parent == "Sundry Debtors",
                        master_id=ledger.master_id,
                    )
                )
        return Masters(ledgers=ledgers, parties=parties, groups=sorted(groups))

    async def get_report(
        self,
        report_name: str,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
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

    async def get_outstanding(
        self,
        receivable: bool = True,
        company: str | None = None,
        as_on: date | None = None,
    ) -> list[OutstandingBill]:
        report = "Bills Receivable" if receivable else "Bills Payable"
        rows = await self.get_report(report, company=company, to_date=as_on)
        today = as_on or date.today()
        bills: list[OutstandingBill] = []
        for row in rows:
            bill_date = from_tally_date(str(row.get("BILLDATE") or ""))
            due = from_tally_date(str(row.get("BILLDUEDATE") or "")) or bill_date
            amount = parsers.to_decimal(
                row.get("CLOSINGBALANCE") or row.get("AMOUNT")
            )
            bills.append(
                OutstandingBill(
                    party_name=str(row.get("PARTYNAME") or row.get("NAME") or ""),
                    bill_reference=str(row.get("NAME") or ""),
                    bill_date=bill_date,
                    amount=abs(amount),
                    due_date=due,
                    overdue_days=(today - due).days if due else 0,
                )
            )
        return bills

    async def get_bank_ledger(
        self,
        ledger_name: str,
        from_date: date,
        to_date: date,
        company: str | None = None,
    ) -> list[LedgerEntry]:
        rows = await self.client.export_report(
            "Ledger Vouchers",
            "VOUCHER",
            company=company,
            from_date=from_date,
            to_date=to_date,
            extra_vars={"SVLEDGERNAME": ledger_name},
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

        if result.errors or not (result.created or result.altered):
            idempotency.record_failure(
                self.store, idempotency_key, "; ".join(result.messages) or "no rows written"
            )
            return WriteResult(
                ok=False,
                idempotency_key=idempotency_key,
                errors=result.messages or ["Tally wrote nothing and gave no reason"],
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
        amended = voucher.model_copy(update={"master_id": master_id})
        element = builders.build_voucher_element(
            amended, action="Alter", remote_id=master_id
        )
        return await self._import_once([element], "Vouchers", idempotency_key, company)

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
        rows = await self.get_report("Trial Balance", company=company)
        return {
            str(row.get("NAME") or ""): parsers.to_decimal(row.get("CLOSINGBALANCE"))
            for row in rows
            if row.get("NAME")
        }
