"""The deferred-ITC ledger: credit waiting for a supplier, until it can be claimed.

A purchase that is in the books but not in this month's GSTR-2B cannot be
claimed this month. It can be claimed the month the supplier reports it - if
somebody remembers. Across thirty clients and twelve months nobody does, and
the credit lapses at the section 16(4) deadline. This remembers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import DeferredItcRow

#: Warn this far ahead of the last day a credit can be claimed.
DEADLINE_WARNING = timedelta(days=60)


@dataclass(frozen=True, slots=True)
class Deferred:
    supplier: str
    supplier_gstin: str
    invoice_no: str
    invoice_date: str
    tax: Decimal
    first_period: str
    status: str
    resolved_period: str = ""


@dataclass(slots=True)
class LedgerUpdate:
    added: list[Deferred]
    now_claimable: list[Deferred]
    still_open: list[Deferred]
    near_deadline: list[tuple[Deferred, date]]

    @property
    def claimable_tax(self) -> Decimal:
        return sum((d.tax for d in self.now_claimable), Decimal("0"))

    @property
    def open_tax(self) -> Decimal:
        return sum((d.tax for d in self.still_open), Decimal("0"))


class DeferredItcLedger:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def update(self, company: str, reconciliation: Any, today: date) -> LedgerUpdate:
        """Fold one month's reconciliation into the ledger.

        Unfiled invoices are added (once - re-running a month does not double
        them); invoices on the ledger that now match 2B become claimable this
        period; whatever is still open is checked against its deadline.
        """
        from tallyagent_tools import itc

        period = reconciliation.period
        added: list[Deferred] = []
        claimable: list[Deferred] = []
        now = datetime.now(UTC)
        with Session(self.engine) as session:
            rows = {
                (r.supplier_gstin.upper(), r.invoice_key): r
                for r in session.exec(
                    select(DeferredItcRow).where(DeferredItcRow.company == company)
                ).all()
            }
            for finding in reconciliation.by(itc.NOT_FILED):
                key = (finding.supplier_gstin.upper(), itc.invoice_key(finding.number))
                row = rows.get(key)
                if row is None:
                    row = DeferredItcRow(
                        company=company,
                        supplier_gstin=finding.supplier_gstin,
                        supplier=finding.supplier,
                        invoice_no=finding.number,
                        invoice_key=key[1],
                        invoice_date=finding.invoice_date.isoformat()
                        if finding.invoice_date
                        else "",
                        tax=format(finding.itc_at_risk, "f"),
                        first_period=period,
                    )
                    rows[key] = row
                    added.append(_deferred(row))
                row.last_seen_period = period
                row.updated_at = now
                session.add(row)

            for finding in reconciliation.by(itc.MATCHED):
                key = (finding.supplier_gstin.upper(), itc.invoice_key(finding.number))
                row = rows.get(key)
                if row is not None and row.status == "open":
                    row.status = "claimable"
                    row.resolved_period = period
                    row.updated_at = now
                    session.add(row)
                    claimable.append(_deferred(row))
            session.commit()

            still_open = [
                _deferred(r) for r in rows.values() if r.status == "open"
            ]

        near: list[tuple[Deferred, date]] = []
        for item in still_open:
            when = _iso(item.invoice_date)
            if when is None:
                continue
            deadline = itc.section_16_4_deadline(when)
            if deadline - today <= DEADLINE_WARNING:
                near.append((item, deadline))
        return LedgerUpdate(added, claimable, still_open, near)

    def open(self, company: str) -> list[Deferred]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(DeferredItcRow).where(
                    DeferredItcRow.company == company, DeferredItcRow.status == "open"
                )
            ).all()
        return [_deferred(r) for r in rows]


def _deferred(row: DeferredItcRow) -> Deferred:
    return Deferred(
        supplier=row.supplier,
        supplier_gstin=row.supplier_gstin,
        invoice_no=row.invoice_no,
        invoice_date=row.invoice_date,
        tax=Decimal(row.tax or "0"),
        first_period=row.first_period,
        status=row.status,
        resolved_period=row.resolved_period,
    )


def _iso(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None
