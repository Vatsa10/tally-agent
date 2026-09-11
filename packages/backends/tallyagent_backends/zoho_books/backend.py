"""Zoho Books adapter — deliberately unimplemented.

It exists to prove the AccountingBackend boundary is real, and it fails loudly
rather than half-working. Anything that silently degraded here would show a CA
firm a trial balance that is not their trial balance.
"""

from __future__ import annotations

from datetime import date

from tallyagent_backends.accounting_backend import (
    LedgerEntry,
    Masters,
    OutstandingBill,
    WriteResult,
)
from tallyagent_core.models import Ledger, Voucher

_MESSAGE = (
    "The Zoho Books backend is not implemented. tallyagent currently supports "
    "TallyPrime only (set [tally] in config.toml). The AccountingBackend "
    "protocol in tallyagent_backends.accounting_backend is the contract to "
    "implement: see packages/tally/tallyagent_tally/backend.py for a worked "
    "example."
)


class ZohoBooksBackend:
    name = "zoho_books"

    def __init__(self, organization_id: str = "", **_: object) -> None:
        self.organization_id = organization_id

    def _unsupported(self, operation: str) -> NotImplementedError:
        return NotImplementedError(f"{operation}: {_MESSAGE}")

    async def probe(self) -> dict[str, str]:
        raise self._unsupported("probe")

    async def list_companies(self) -> list[str]:
        raise self._unsupported("list_companies")

    async def get_masters(self, company: str | None = None) -> Masters:
        raise self._unsupported("get_masters")

    async def get_report(
        self,
        report_name: str,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        raise self._unsupported("get_report")

    async def create_voucher(
        self, voucher: Voucher, idempotency_key: str, company: str | None = None
    ) -> WriteResult:
        raise self._unsupported("create_voucher")

    async def alter_voucher(
        self,
        voucher: Voucher,
        master_id: str,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult:
        raise self._unsupported("alter_voucher")

    async def create_ledger(
        self, ledger: Ledger, idempotency_key: str, company: str | None = None
    ) -> WriteResult:
        raise self._unsupported("create_ledger")

    async def get_outstanding(
        self,
        receivable: bool = True,
        company: str | None = None,
        as_on: date | None = None,
    ) -> list[OutstandingBill]:
        raise self._unsupported("get_outstanding")

    async def get_bank_ledger(
        self,
        ledger_name: str,
        from_date: date,
        to_date: date,
        company: str | None = None,
    ) -> list[LedgerEntry]:
        raise self._unsupported("get_bank_ledger")
