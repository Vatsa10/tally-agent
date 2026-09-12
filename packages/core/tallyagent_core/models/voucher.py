from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# GST rates notified under the CGST Act that we accept. Anything else is a typo
# or an unsupported scheme and must not reach Tally.
VALID_GST_RATES: frozenset[Decimal] = frozenset(
    Decimal(r) for r in ("0", "0.1", "0.25", "3", "5", "12", "18", "28")
)

PAISA = Decimal("0.01")


class VoucherType(StrEnum):
    SALES = "Sales"
    PURCHASE = "Purchase"
    PAYMENT = "Payment"
    RECEIPT = "Receipt"
    JOURNAL = "Journal"
    CONTRA = "Contra"


class GSTDetails(BaseModel):
    """Tax split for one voucher. Exactly one of (CGST+SGST) or IGST is used."""

    rate: Decimal = Decimal("0")
    taxable_value: Decimal = Decimal("0")
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")
    cess: Decimal = Decimal("0")
    place_of_supply: str | None = None
    hsn: str | None = None

    @field_validator("rate")
    @classmethod
    def _known_rate(cls, v: Decimal) -> Decimal:
        if v not in VALID_GST_RATES:
            raise ValueError(
                f"GST rate {v} not in {sorted(VALID_GST_RATES)}"
            )
        return v

    @property
    def is_interstate(self) -> bool:
        return self.igst > 0

    @property
    def total_tax(self) -> Decimal:
        return self.cgst + self.sgst + self.igst + self.cess


class VoucherLine(BaseModel):
    """One ledger entry. Positive amount = debit, negative = credit.

    This is *our* sign convention and is deliberately the plain-English one.
    Tally's own convention (negated amounts plus ISDEEMEDPOSITIVE) is applied
    only at the XML boundary — see tallyagent_tally.xml.builders.
    """

    ledger_name: str = Field(min_length=1)
    amount: Decimal
    is_debit: bool | None = None
    cost_centre: str | None = None
    bill_reference: str | None = None

    def model_post_init(self, _ctx: object) -> None:
        if self.is_debit is None:
            object.__setattr__(self, "is_debit", self.amount >= 0)

    @property
    def debit(self) -> Decimal:
        return self.amount if self.amount > 0 else Decimal("0")

    @property
    def credit(self) -> Decimal:
        return -self.amount if self.amount < 0 else Decimal("0")


class Voucher(BaseModel):
    """A voucher draft, before it has ever touched Tally."""

    voucher_type: VoucherType
    date: date
    lines: list[VoucherLine] = Field(min_length=1)
    party_name: str | None = None
    narration: str = ""
    reference: str = ""
    gst: GSTDetails | None = None
    voucher_number: str | None = None
    master_id: str | None = None
    guid: str | None = None
    #: The identity we assign at creation so the voucher stays addressable.
    #: Tally's own MASTERID and GUID cannot be used to amend or delete on
    #: TallyPrime 1.x - an Alter addressed by either silently creates a
    #: duplicate. Defaults to the idempotency key at post time.
    remote_id: str | None = None

    @property
    def total_debit(self) -> Decimal:
        return sum((ln.debit for ln in self.lines), Decimal("0"))

    @property
    def total_credit(self) -> Decimal:
        return sum((ln.credit for ln in self.lines), Decimal("0"))

    @property
    def is_balanced(self) -> bool:
        """Balanced to the paisa — not to float tolerance."""
        return self.total_debit.quantize(PAISA) == self.total_credit.quantize(PAISA)

    @property
    def amount(self) -> Decimal:
        """Voucher value = the debit side total."""
        return self.total_debit.quantize(PAISA)

    def fingerprint(self) -> str:
        """Stable content hash: same economic voucher -> same fingerprint.

        Feeds both duplicate detection and the idempotency key. Line order must
        not change the answer, so lines are sorted before hashing.
        """
        lines = sorted(
            f"{ln.ledger_name}|{ln.amount.quantize(PAISA)}" for ln in self.lines
        )
        payload = "|".join(
            [
                self.voucher_type.value,
                self.date.isoformat(),
                self.party_name or "",
                self.reference or "",
                *lines,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
