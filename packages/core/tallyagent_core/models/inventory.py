"""Stock: the second dimension a trading company's vouchers move.

An accounting-only voucher says a sale was worth 59,000. A trader also needs to
know that it moved 20 boxes out of the main godown at 2,500 each. These models
carry that, and the invariant that ties the two together is
``quantity * rate == amount``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

#: Quantities are tracked to three decimals. Tally allows a configurable number
#: of decimal places per unit; three covers grams, litres and fractional hours
#: without inviting float-shaped nonsense.
QUANTITY = Decimal("0.001")
PAISA = Decimal("0.01")


class Unit(BaseModel):
    """A unit of measure. ``Nos``, ``Kg``, ``Box``."""

    name: str = Field(min_length=1)
    formal_name: str = ""
    decimal_places: int = 0
    master_id: str | None = None

    def model_post_init(self, _ctx: object) -> None:
        if not self.formal_name:
            object.__setattr__(self, "formal_name", self.name)


class StockGroup(BaseModel):
    """A grouping of stock items, mirroring Tally's stock group tree."""

    name: str = Field(min_length=1)
    parent: str = ""
    master_id: str | None = None


class Godown(BaseModel):
    """A location stock sits in. Tally calls it a godown; some call it a store."""

    name: str = Field(min_length=1)
    parent: str = ""
    master_id: str | None = None


class StockItem(BaseModel):
    """One thing a company buys and sells."""

    name: str = Field(min_length=1)
    parent: str = ""
    base_units: str = "Nos"
    hsn_code: str | None = None
    gst_rate: Decimal | None = None
    opening_quantity: Decimal = Decimal("0")
    opening_rate: Decimal = Decimal("0")
    master_id: str | None = None

    @property
    def opening_value(self) -> Decimal:
        return (self.opening_quantity * self.opening_rate).quantize(PAISA)

    @field_validator("gst_rate")
    @classmethod
    def _known_rate(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return v
        from tallyagent_core.models.voucher import VALID_GST_RATES

        if v not in VALID_GST_RATES:
            raise ValueError(
                f"GST rate {v} is not a notified rate "
                f"({', '.join(str(r) for r in sorted(VALID_GST_RATES))})"
            )
        return v


class InventoryLine(BaseModel):
    """One stock movement on a voucher.

    Sign convention matches the ledger side: positive quantity is stock coming
    **in** (a purchase), negative is stock going **out** (a sale). ``amount`` is
    derived from quantity and rate rather than supplied, because three numbers
    that must agree are three numbers that can disagree.
    """

    stock_item: str = Field(min_length=1)
    quantity: Decimal
    rate: Decimal
    unit: str = ""
    godown: str = ""
    #: Override only when a document's line total genuinely differs from
    #: quantity * rate - a rounded invoice line, say. Validation surfaces it.
    amount_override: Decimal | None = None

    @property
    def amount(self) -> Decimal:
        """Value of the movement, always positive."""
        if self.amount_override is not None:
            return abs(self.amount_override).quantize(PAISA)
        return abs(self.quantity * self.rate).quantize(PAISA)

    @property
    def is_inward(self) -> bool:
        return self.quantity > 0

    @property
    def computed_amount(self) -> Decimal:
        return abs(self.quantity * self.rate).quantize(PAISA)

    @property
    def overrides_value(self) -> bool:
        """Does the stated amount disagree with quantity x rate?"""
        if self.amount_override is None:
            return False
        return abs(self.amount_override).quantize(PAISA) != self.computed_amount
