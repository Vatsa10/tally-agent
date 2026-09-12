"""Stage 15.3: cost centres - masters, allocation, validation and reporting."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.models import CostCategory, CostCentre, Voucher, VoucherLine, VoucherType
from tallyagent_core.policy import Policy
from tallyagent_core.validation import ValidationContext, validate
from tallyagent_tally.xml import builders
from tallyagent_tools import cost_centres, vouchers
from tallyagent_tools.base import ToolContext
from tallyagent_tools.executor import build_executor


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def queue(engine):
    return ApprovalQueue(engine, AuditLog(engine))


@pytest.fixture
def ctx(queue, backend, company):
    tools = ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=queue.enqueue
    )
    queue.executor = build_executor(tools)
    return tools


async def seed(ctx, queue) -> None:
    result = await cost_centres.create_cost_centres(
        ctx,
        categories=["Branches"],
        centres=[
            {"name": "Ahmedabad", "category": "Branches"},
            {"name": "Surat", "category": "Branches"},
        ],
    )
    await queue.approve(result.data["ticket"], "test")


# --- XML --------------------------------------------------------------------


def test_a_category_carries_its_allocation_flags():
    element = builders.build_cost_category_element(
        CostCategory(name="Branches", allocate_revenue=True, allocate_non_revenue=False)
    )
    assert element.get("NAME") == "Branches"
    assert element.findtext("ALLOCATEREVENUE") == "Yes"
    assert element.findtext("ALLOCATENONREVENUE") == "No"


def test_a_centre_names_the_category_it_belongs_to():
    element = builders.build_cost_centre_element(
        CostCentre(name="Ahmedabad", category="Branches")
    )
    assert element.findtext("CATEGORY") == "Branches"


def test_an_allocation_is_filed_under_the_lines_own_category():
    """Not the Primary one: Tally refuses an allocation in the wrong category."""
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(
                ledger_name="Purchase - GST 18%",
                amount=Decimal("10000.00"),
                cost_centre="Ahmedabad",
                cost_category="Branches",
            ),
            VoucherLine(ledger_name="Cash", amount=Decimal("-10000.00")),
        ],
    )
    element = builders.build_voucher_element(voucher)
    category = element.find(".//CATEGORYALLOCATIONS.LIST")
    assert category.findtext("CATEGORY") == "Branches"
    assert category.find("COSTCENTREALLOCATIONS.LIST").findtext("NAME") == "Ahmedabad"


def test_a_line_without_a_cost_centre_carries_no_allocation():
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Purchase - GST 18%", amount=Decimal("10000.00")),
            VoucherLine(ledger_name="Cash", amount=Decimal("-10000.00")),
        ],
    )
    assert builders.build_voucher_element(voucher).find(".//CATEGORYALLOCATIONS.LIST") is None


# --- validation -------------------------------------------------------------


def _voucher(centre: str | None) -> Voucher:
    return Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(
                ledger_name="Purchase - GST 18%",
                amount=Decimal("10000.00"),
                cost_centre=centre,
            ),
            VoucherLine(ledger_name="Cash", amount=Decimal("-10000.00")),
        ],
    )


def test_an_unknown_cost_centre_is_a_failure(company):
    report = validate(
        _voucher("Baroda"),
        ValidationContext(
            company=company,
            known_ledgers={"Purchase - GST 18%", "Cash"},
            known_cost_centres={"Ahmedabad": "Branches"},
        ),
    )
    rule = next(r for r in report.results if r.rule == "cost_centres_exist")
    assert not rule.passed
    assert "Baroda" in rule.message


def test_allocating_when_the_company_tracks_none_is_a_failure(company):
    report = validate(
        _voucher("Ahmedabad"),
        ValidationContext(company=company, known_ledgers={"Purchase - GST 18%", "Cash"}),
    )
    rule = next(r for r in report.results if r.rule == "cost_centres_exist")
    assert not rule.passed
    assert "tracks no cost centres" in rule.message


def test_no_cost_centre_asked_for_is_no_complaint(company):
    report = validate(
        _voucher(None),
        ValidationContext(company=company, known_ledgers={"Purchase - GST 18%", "Cash"}),
    )
    rule = next(r for r in report.results if r.rule == "cost_centres_exist")
    assert rule.passed


# --- masters and reporting --------------------------------------------------


async def test_masters_are_created_as_one_approval(ctx, queue):
    await seed(ctx, queue)
    listed = await cost_centres.list_cost_centres(ctx)
    assert {row["name"] for row in listed.data} == {"Ahmedabad", "Surat"}
    assert all(row["category"] == "Branches" for row in listed.data)


async def test_seeding_the_same_centres_twice_is_a_no_op(ctx, queue):
    await seed(ctx, queue)
    again = await cost_centres.create_cost_centres(
        ctx, categories=["Branches"], centres=[{"name": "Ahmedabad", "category": "Branches"}]
    )
    assert again.pending is None
    assert "already exist" in again.message


async def test_a_centre_in_an_unknown_category_is_refused(ctx, queue):
    """The category must already be in the company, not merely in the envelope."""
    result = await cost_centres.create_cost_centres(
        ctx, centres=[{"name": "Rajkot", "category": "Regions"}]
    )
    write = await queue.approve(result.data["ticket"], "test")
    assert not write.ok
    assert any("Regions" in e for e in write.errors)


async def test_a_journal_allocates_to_a_cost_centre(ctx, queue):
    await seed(ctx, queue)
    result = await vouchers.create_journal(
        ctx,
        lines=[
            {"ledger": "Purchase - GST 18%", "amount": "10000.00", "cost_centre": "Ahmedabad"},
            {"ledger": "Cash", "amount": "-10000.00"},
        ],
        voucher_date=date(2026, 6, 1),
        narration="June rent, Ahmedabad branch",
    )
    assert result.pending.voucher.lines[0].cost_category == "Branches"
    assert result.data is not None, result.message + " | " + result.validation.summary()
    await queue.approve(result.data["ticket"], "test")

    summary = await cost_centres.cost_centre_summary(ctx)
    rows = {row["cost_centre"]: row["net"] for row in summary.data}
    assert rows["Ahmedabad"] == "10000.00"
    assert rows["Surat"] == "0.00"


async def test_a_journal_naming_an_unknown_centre_never_reaches_tally(ctx, queue):
    await seed(ctx, queue)
    result = await vouchers.create_journal(
        ctx,
        lines=[
            {"ledger": "Purchase - GST 18%", "amount": "10000.00", "cost_centre": "Baroda"},
            {"ledger": "Cash", "amount": "-10000.00"},
        ],
        voucher_date=date(2026, 6, 1),
    )
    assert result.data is None
    assert "cost_centres_exist" in result.validation.summary()


async def test_a_company_with_no_cost_centres_says_so(ctx):
    summary = await cost_centres.cost_centre_summary(ctx)
    assert "tracks no cost centres" in summary.message
    assert summary.data == []
