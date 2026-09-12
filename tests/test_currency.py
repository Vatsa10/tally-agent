"""Stage 15.4: foreign currency, and the build that cannot take it."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.models import Currency, Voucher, VoucherLine, VoucherType
from tallyagent_core.models.currency import base_amount
from tallyagent_core.policy import Policy
from tallyagent_core.validation import ValidationContext, validate
from tallyagent_tally.xml import builders
from tallyagent_tools import currencies
from tallyagent_tools.base import ToolContext
from tallyagent_tools.executor import build_executor


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def queue(engine):
    return ApprovalQueue(engine, AuditLog(engine))


def _enable(backend) -> None:
    """Point the tools at a Tally that claims to survive a currency import."""
    backend.client.config = replace(backend.client.config, supports_multi_currency=True)


@pytest.fixture
def ctx(queue, backend, company):
    tools = ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=queue.enqueue
    )
    queue.executor = build_executor(tools)
    return tools


def _export_voucher(rate: Decimal | None = Decimal("83.25")) -> Voucher:
    return Voucher(
        voucher_type=VoucherType.SALES,
        date=date(2026, 6, 1),
        party_name="Acme Industries",
        lines=[
            VoucherLine(
                ledger_name="Sales - GST 18%",
                amount=Decimal("-83250.00"),
                currency="USD",
                rate_of_exchange=rate,
            ),
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("83250.00")),
        ],
    )


# --- model ------------------------------------------------------------------


def test_the_foreign_amount_is_derived_from_the_base_one():
    line = VoucherLine(
        ledger_name="Sales - GST 18%",
        amount=Decimal("-83250.00"),
        currency="USD",
        rate_of_exchange=Decimal("83.25"),
    )
    assert line.foreign_amount == Decimal("-1000.00")


def test_a_base_currency_line_has_no_foreign_amount():
    line = VoucherLine(ledger_name="Cash", amount=Decimal("100.00"))
    assert line.foreign_amount is None


def test_base_amount_rounds_to_the_paisa():
    assert base_amount(Decimal("1000"), Decimal("83.256")) == Decimal("83256.00")


def test_a_currency_formal_name_defaults_to_its_symbol():
    assert Currency(name="USD").formal_name == "USD"


# --- XML --------------------------------------------------------------------


def test_a_foreign_line_carries_the_rate_and_the_forex_amount():
    entry = builders.build_voucher_element(_export_voucher()).find(
        "ALLLEDGERENTRIES.LIST"
    )
    assert entry.findtext("RATEOFEXCHANGE") == "83.25"
    # Negated like every other Tally amount: a credit is positive to us.
    assert entry.findtext("FOREXAMOUNT") == "1000.00"


def test_a_base_currency_line_carries_neither():
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("100.00")),
            VoucherLine(ledger_name="Round Off", amount=Decimal("-100.00")),
        ],
    )
    entry = builders.build_voucher_element(voucher).find("ALLLEDGERENTRIES.LIST")
    assert entry.find("RATEOFEXCHANGE") is None
    assert entry.find("FOREXAMOUNT") is None


def test_a_currency_master_omits_a_formal_name_equal_to_its_symbol():
    """Same trap as units: Tally calls that a duplicate original name."""
    element = builders.build_currency_element(Currency(name="USD"))
    assert element.find("MAILINGNAME") is None
    assert element.findtext("DECIMALPLACES") == "2"


# --- validation -------------------------------------------------------------


def test_foreign_currency_is_refused_on_a_tally_that_cannot_take_it(company):
    report = validate(
        _export_voucher(),
        ValidationContext(
            company=company,
            known_ledgers={"Sales - GST 18%", "Acme Industries"},
            known_currencies={"USD"},
            multi_currency_supported=False,
        ),
    )
    rule = next(r for r in report.results if r.rule == "currency_is_usable")
    assert not rule.passed
    assert "terminates" in rule.message


def test_an_unknown_currency_is_refused(company):
    report = validate(
        _export_voucher(),
        ValidationContext(
            company=company,
            known_ledgers={"Sales - GST 18%", "Acme Industries"},
            known_currencies={"EUR"},
            multi_currency_supported=True,
        ),
    )
    rule = next(r for r in report.results if r.rule == "currency_is_usable")
    assert not rule.passed
    assert "USD" in rule.message


def test_a_missing_rate_of_exchange_is_refused(company):
    report = validate(
        _export_voucher(rate=None),
        ValidationContext(
            company=company,
            known_ledgers={"Sales - GST 18%", "Acme Industries"},
            known_currencies={"USD"},
            multi_currency_supported=True,
        ),
    )
    rule = next(r for r in report.results if r.rule == "currency_is_usable")
    assert not rule.passed
    assert "rate of exchange" in rule.message


def test_a_complete_foreign_line_passes(company):
    report = validate(
        _export_voucher(),
        ValidationContext(
            company=company,
            known_ledgers={"Sales - GST 18%", "Acme Industries"},
            known_currencies={"USD"},
            multi_currency_supported=True,
        ),
    )
    rule = next(r for r in report.results if r.rule == "currency_is_usable")
    assert rule.passed


def test_a_rupee_only_voucher_never_consults_the_rule(company):
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("100.00")),
            VoucherLine(ledger_name="Round Off", amount=Decimal("-100.00")),
        ],
    )
    report = validate(
        voucher,
        ValidationContext(company=company, known_ledgers={"Cash", "Round Off"}),
    )
    rule = next(r for r in report.results if r.rule == "currency_is_usable")
    assert rule.passed


# --- masters ----------------------------------------------------------------


async def test_creating_a_currency_is_refused_and_nothing_is_sent(ctx):
    result = await currencies.create_currencies(
        ctx, currencies=[{"symbol": "USD", "name": "US Dollar"}]
    )
    assert result.pending is None
    assert "terminates the process" in result.message
    # Nothing queued, and nothing in Tally either.
    assert (await currencies.list_currencies(ctx)).data == []


async def test_a_currency_is_created_when_the_build_supports_it(ctx, queue, backend):
    _enable(backend)
    result = await currencies.create_currencies(
        ctx, currencies=[{"symbol": "USD", "name": "US Dollar"}]
    )
    write = await queue.approve(result.data["ticket"], "test")
    assert write.ok, write.errors

    listed = await currencies.list_currencies(ctx)
    assert [row["symbol"] for row in listed.data] == ["USD"]


async def test_an_approval_queued_before_the_flag_went_off_still_refuses(
    ctx, queue, backend
):
    """The executor is the call that would kill Tally, so it checks too."""
    _enable(backend)
    result = await currencies.create_currencies(ctx, currencies=[{"symbol": "USD"}])
    backend.client.config = replace(
        backend.client.config, supports_multi_currency=False
    )
    write = await queue.approve(result.data["ticket"], "test")
    assert not write.ok
    assert any("terminates the process" in e for e in write.errors)


async def test_creating_the_same_currency_twice_is_a_no_op(ctx, queue, backend):
    _enable(backend)
    first = await currencies.create_currencies(ctx, currencies=[{"symbol": "USD"}])
    await queue.approve(first.data["ticket"], "test")
    again = await currencies.create_currencies(ctx, currencies=[{"symbol": "USD"}])
    assert again.pending is None
    assert "already exist" in again.message
