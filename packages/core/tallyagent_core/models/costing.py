"""Cost centres: the third dimension a voucher can carry.

Accounting says what was spent and on which ledger. Inventory says what goods
moved. Cost centres say *which part of the business* it belonged to - the
Ahmedabad branch, the Q3 campaign, the job for a particular client. Nothing
else in the books answers "what did that branch actually cost us".
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Tally's built-in category, used when a company tracks cost centres without
#: grouping them. Always present; never created by us.
PRIMARY_CATEGORY = "Primary Cost Category"


class CostCategory(BaseModel):
    """A dimension along which costs are split - Branches, Projects, Campaigns.

    A voucher line can be allocated once per category, which is how a cost can
    be both "Ahmedabad" and "Q3 campaign" without double counting.
    """

    name: str = Field(min_length=1)
    allocate_revenue: bool = True
    allocate_non_revenue: bool = True
    master_id: str | None = None


class CostCentre(BaseModel):
    """One value within a category. ``Ahmedabad`` inside ``Branches``."""

    name: str = Field(min_length=1)
    parent: str = ""
    category: str = PRIMARY_CATEGORY
    master_id: str | None = None
