"""Remembering the REMOTEIDs we assigned to vouchers we posted.

Tally honours a REMOTEID supplied on create, for the life of the voucher, but
never reports it back: every export shows Tally's own GUID. The handle that
makes a voucher amendable therefore lives here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import VoucherIndexRow


@dataclass(slots=True)
class IndexedVoucher:
    remote_id: str
    master_id: str
    voucher_number: str
    voucher_type: str
    reference: str
    voucher_date: str
    amount: str


class VoucherIndex:
    """Our side of the voucher identity mapping."""

    def __init__(self, engine: Engine, company: str) -> None:
        self.engine = engine
        self.company = company

    def record(
        self,
        remote_id: str,
        master_id: str = "",
        voucher_number: str = "",
        voucher_type: str = "",
        reference: str = "",
        voucher_date: str = "",
        amount: str = "0",
    ) -> None:
        """Note a voucher we posted. Re-recording the same id updates it."""
        if not remote_id:
            return
        with Session(self.engine) as session:
            row = session.exec(
                select(VoucherIndexRow)
                .where(VoucherIndexRow.company == self.company)
                .where(VoucherIndexRow.remote_id == remote_id)
            ).first()
            if row is None:
                row = VoucherIndexRow(company=self.company, remote_id=remote_id)
            row.master_id = master_id or row.master_id
            row.voucher_number = voucher_number or row.voucher_number
            row.voucher_type = voucher_type or row.voucher_type
            row.reference = reference or row.reference
            row.voucher_date = voucher_date or row.voucher_date
            row.amount = amount or row.amount
            row.deleted = False
            session.add(row)
            session.commit()

    def mark_deleted(self, remote_id: str) -> None:
        with Session(self.engine) as session:
            row = session.exec(
                select(VoucherIndexRow)
                .where(VoucherIndexRow.company == self.company)
                .where(VoucherIndexRow.remote_id == remote_id)
            ).first()
            if row is not None:
                row.deleted = True
                session.add(row)
                session.commit()

    def all(self) -> list[IndexedVoucher]:
        with Session(self.engine) as session:
            rows = list(
                session.exec(
                    select(VoucherIndexRow)
                    .where(VoucherIndexRow.company == self.company)
                    .where(VoucherIndexRow.deleted == False)  # noqa: E712 - SQL, not Python
                )
            )
        return [
            IndexedVoucher(
                remote_id=row.remote_id,
                master_id=row.master_id,
                voucher_number=row.voucher_number,
                voucher_type=row.voucher_type,
                reference=row.reference,
                voucher_date=row.voucher_date,
                amount=row.amount,
            )
            for row in rows
        ]

    def by_master_id(self) -> dict[str, str]:
        """Tally's MASTERID -> our REMOTEID. The join a lookup needs."""
        return {v.master_id: v.remote_id for v in self.all() if v.master_id}

    def by_reference(self) -> dict[str, str]:
        return {v.reference: v.remote_id for v in self.all() if v.reference}
