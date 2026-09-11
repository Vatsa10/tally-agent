"""The accounting-backend boundary.

Everything above this line (tools, agent, approvals, channels) is written
against this protocol, so adding Zoho Books or Busy later is an adapter, not a
rewrite. Tally is implemented fully; Zoho Books is a skeleton that fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tallyagent_core.models import Ledger, Party, Voucher


@dataclass(slots=True)
class WriteResult:
    """Outcome of a mutating call."""

    ok: bool
    voucher_number: str = ""
    master_id: str = ""
    idempotency_key: str = ""
    replayed: bool = False
    errors: list[str] = field(default_factory=list)
    raw_request: str = ""


@dataclass(slots=True)
class Masters:
    """A snapshot of the chart of accounts, as validation sees it."""

    ledgers: list[Ledger] = field(default_factory=list)
    parties: list[Party] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)

    @property
    def ledger_names(self) -> set[str]:
        return {ledger.name for ledger in self.ledgers}


@dataclass(slots=True)
class OutstandingBill:
    party_name: str
    bill_reference: str
    bill_date: date | None
    amount: Decimal
    due_date: date | None = None
    overdue_days: int = 0

    def ageing_bucket(self) -> str:
        """Standard receivable ageing buckets used across Indian practice."""
        days = self.overdue_days
        if days <= 0:
            return "not due"
        if days <= 30:
            return "0-30"
        if days <= 60:
            return "31-60"
        if days <= 90:
            return "61-90"
        return "90+"


@dataclass(slots=True)
class LedgerEntry:
    """One posting against a ledger, as a report row."""

    date: date | None
    voucher_type: str
    voucher_number: str
    ledger_name: str
    particulars: str
    amount: Decimal
    narration: str = ""
    master_id: str = ""


@runtime_checkable
class AccountingBackend(Protocol):
    """The full surface an accounting system must provide.

    Read methods may be called freely. Write methods must be idempotent on
    ``idempotency_key`` and must never be called without a passing
    ValidationReport - the tools layer enforces that, not the adapter.
    """

    name: str

    async def probe(self) -> dict[str, str]: ...

    async def list_companies(self) -> list[str]: ...

    async def get_masters(self, company: str | None = None) -> Masters: ...

    async def get_report(
        self,
        report_name: str,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict[str, object]]: ...

    async def create_voucher(
        self,
        voucher: Voucher,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult: ...

    async def alter_voucher(
        self,
        voucher: Voucher,
        master_id: str,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult: ...

    async def create_ledger(
        self,
        ledger: Ledger,
        idempotency_key: str,
        company: str | None = None,
    ) -> WriteResult: ...

    async def get_outstanding(
        self,
        receivable: bool = True,
        company: str | None = None,
        as_on: date | None = None,
    ) -> list[OutstandingBill]: ...

    async def get_bank_ledger(
        self,
        ledger_name: str,
        from_date: date,
        to_date: date,
        company: str | None = None,
    ) -> list[LedgerEntry]: ...
