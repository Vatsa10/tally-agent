"""Foreign currency, for a company that bills or buys abroad.

The base-currency amount is the one that balances. A foreign amount and a rate
of exchange ride alongside it, so the debit==credit rule is untouched and every
report stays in rupees.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class Currency(BaseModel):
    """A currency master. ``name`` is the symbol Tally shows, e.g. ``USD``."""

    name: str = Field(min_length=1)
    formal_name: str = ""
    decimal_places: int = 2
    #: Suffix currencies print after the number (``100 USD``), prefix before it.
    is_suffix: bool = False
    has_space: bool = True
    master_id: str | None = None

    def model_post_init(self, _ctx: object) -> None:
        if not self.formal_name:
            object.__setattr__(self, "formal_name", self.name)


def base_amount(foreign: Decimal, rate_of_exchange: Decimal) -> Decimal:
    """What a foreign amount is worth in the base currency."""
    from tallyagent_core.models.voucher import PAISA

    return (foreign * rate_of_exchange).quantize(PAISA)
