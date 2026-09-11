from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class Group(BaseModel):
    """A Tally account group (``Sundry Debtors``, ``Duties & Taxes``, ...)."""

    name: str = Field(min_length=1)
    parent: str = ""
    is_revenue: bool | None = None
    master_id: str | None = None


class Ledger(BaseModel):
    """A Tally ledger master, as exported from ``List of Ledgers``."""

    name: str = Field(min_length=1)
    parent: str = ""
    opening_balance: Decimal = Decimal("0")
    gstin: str | None = None
    gst_registration_type: str | None = None
    state: str | None = None
    master_id: str | None = None
    alias: str | None = None

    @property
    def is_party(self) -> bool:
        return self.parent in {"Sundry Debtors", "Sundry Creditors"}


class Party(BaseModel):
    """A customer or supplier: a ledger plus the trade terms we care about."""

    name: str = Field(min_length=1)
    ledger_name: str = ""
    gstin: str | None = None
    state_code: str | None = None
    credit_period_days: int = 0
    is_customer: bool = True
    master_id: str | None = None

    def model_post_init(self, _ctx: object) -> None:
        if not self.ledger_name:
            object.__setattr__(self, "ledger_name", self.name)
