"""Stage 14.3: the whole EDU bootstrap, against the fake Tally.

The same ``scenarios/e2e_edu_bootstrap.yaml`` that ``scripts/live_e2e.py`` runs
against a real instance. What CI can assert and a live run cannot is the
*negative* space: that nothing posted before approval, that a non-TA company is
refused, that an EDU-illegal date never reaches Tally.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tallyagent_agent.memory import Memory
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_channels.services import Services
from tallyagent_channels.tui.script_runner import Scenario, run_scenario
from tallyagent_core.livemode import LiveMode, WriteScopeError
from tallyagent_core.models import Company, Period
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tally.backend import TallyBackend
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.fake_server import FakeTally
from tallyagent_tools import company as company_tools
from tallyagent_tools import masters as master_tools
from tallyagent_tools.base import PendingAction, ToolContext

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = ROOT / "scenarios" / "e2e_edu_bootstrap.yaml"
COMPANY = "TA-Demo Traders"
FY = Period(start=date(2026, 4, 1), end=date(2027, 3, 31))
LIVE_EDU = LiveMode(enabled=True, write_prefix="TA-", edu=True)


@pytest.fixture
def empty_tally() -> FakeTally:
    """A fresh install: server up, no companies, Educational restrictions on."""
    return FakeTally(companies=[], edu=True, supports_company_create=True)


@pytest.fixture
def stubborn_tally() -> FakeTally:
    """The Tally we actually met: XML company creation refused."""
    return FakeTally(companies=[], edu=True, supports_company_create=False)


def wire(tally: FakeTally, live: LiveMode = LIVE_EDU) -> Services:
    engine = make_engine(":memory:")
    audit = AuditLog(engine)
    client = TallyClient(
        TallyConfig(host="127.0.0.1", port=9000), transport=tally.transport
    )
    backend = TallyBackend(client)
    company = Company(name="", state_code="24", gstin="24AAAAA0000A1Z8", period=FY)
    queue = ApprovalQueue(engine, audit)
    tools = ToolContext(
        backend=backend, company=company, policy=Policy.default(), live=live,
        enqueue=queue.enqueue, source="script",
    )

    async def execute(action: PendingAction):
        if action.voucher is None:
            return await master_tools._execute_master(tools, action)
        return await backend.create_voucher(
            action.voucher, action.idempotency_key, tools.company.name
        )

    queue.executor = execute
    return Services(
        company=company,
        tools=tools,
        queue=queue,
        audit=audit,
        router=Router(mock.MockProvider()),
        memory=Memory(engine, COMPANY),
    )


# --- the scenario -----------------------------------------------------------


def test_the_scenario_file_is_the_one_the_live_runner_uses():
    assert SCENARIO.exists()
    scenario = Scenario.load(SCENARIO)
    assert scenario.steps[0].say == "/probe"
    assert any(step.say.startswith("/bootstrap") for step in scenario.steps)
    assert any(step.say.startswith("/seed") for step in scenario.steps)


async def test_the_whole_scenario_passes_against_the_fake(empty_tally):
    services = wire(empty_tally)
    result = await run_scenario(services, Scenario.load(SCENARIO), live=LIVE_EDU)

    assert result.ok, result.report()
    # The company exists in Tally, and it is the one we asked for.
    assert empty_tally.companies == [COMPANY]
    # Masters and vouchers really landed.
    assert "Output IGST" in empty_tally.ledgers
    assert empty_tally.vouchers, "the scenario should leave posted vouchers"
    # Every voucher is on a date Educational mode accepts.
    for voucher in empty_tally.vouchers:
        assert voucher.date is None or voucher.date.day in (1, 2, 31)
    # And the audit chain covering all of it is intact.
    assert services.audit.verify().ok


async def test_the_report_names_every_step(empty_tally):
    services = wire(empty_tally)
    result = await run_scenario(services, Scenario.load(SCENARIO), live=LIVE_EDU)
    report = result.report()
    assert "Result: PASS" in report
    for step in result.scenario.steps:
        assert step.say in report


# --- bootstrap paths --------------------------------------------------------


async def test_bootstrap_over_xml_when_tally_supports_it(empty_tally):
    services = wire(empty_tally)
    result = await company_tools.bootstrap_company(services.tools, COMPANY)
    assert result.data["ok"]
    assert result.data["attempt"] == "xml"
    assert not result.data["used_tier3"]
    assert empty_tally.companies == [COMPANY]
    assert services.tools.company.name == COMPANY


async def test_bootstrap_falls_back_when_xml_is_refused(stubborn_tally):
    """The Tally we actually met. Without a Tier 3 runner it must say so
    clearly rather than pretend."""
    services = wire(stubborn_tally)
    result = await company_tools.bootstrap_company(services.tools, COMPANY)
    assert not result.data["ok"]
    assert "Could not find Company" in " ".join(result.data["xml_errors"])
    assert "Tier 3" in result.message
    assert stubborn_tally.companies == []


async def test_bootstrap_uses_tier3_when_one_is_wired(stubborn_tally):
    services = wire(stubborn_tally)

    class Runner:
        """Stands in for the keyboard path, which a fake cannot be driven by."""

        approve = None

        async def run(self, task: str):
            stubborn_tally.create_company_via_ui(COMPANY)

            class Session:
                completed = True
                stopped_reason = ""

            return Session()

    services.tools.fallback = Runner()
    result = await company_tools.bootstrap_company(services.tools, COMPANY)
    assert result.data["ok"]
    assert result.data["attempt"] == "tier3"
    assert result.data["used_tier3"]
    assert stubborn_tally.companies == [COMPANY]


async def test_bootstrapping_an_existing_company_just_activates_it(empty_tally):
    empty_tally.create_company_via_ui(COMPANY)
    services = wire(empty_tally)
    result = await company_tools.bootstrap_company(services.tools, COMPANY)
    assert result.data["attempt"] == "existing"
    assert services.tools.company.name == COMPANY


# --- the negative space -----------------------------------------------------


async def test_a_non_ta_company_cannot_be_bootstrapped(empty_tally):
    services = wire(empty_tally)
    with pytest.raises(WriteScopeError, match="TA-"):
        await company_tools.bootstrap_company(services.tools, "Real Client Pvt Ltd")
    assert empty_tally.companies == []


async def test_writes_to_a_non_ta_company_are_refused(empty_tally):
    """The guard that protects real books that appear on the same machine."""
    services = wire(empty_tally)
    await company_tools.bootstrap_company(services.tools, COMPANY)
    await company_tools.seed_chart_of_accounts(services.tools)
    await services.queue.approve("APR-0001", "test")

    # Someone points the session at a company we did not create.
    services.tools.company = services.tools.company.model_copy(
        update={"name": "Real Client Pvt Ltd"}
    )
    from tallyagent_tools import vouchers

    before = len(empty_tally.vouchers)
    with pytest.raises(WriteScopeError):
        await vouchers.create_receipt(
            services.tools, "Acme Industries", "100", voucher_date=date(2026, 4, 1)
        )
    assert len(empty_tally.vouchers) == before


async def test_an_edu_illegal_date_never_reaches_tally(empty_tally):
    services = wire(empty_tally)
    await company_tools.bootstrap_company(services.tools, COMPANY)
    await company_tools.seed_chart_of_accounts(services.tools)
    await services.queue.approve("APR-0001", "test")

    from tallyagent_tools import vouchers

    requests_before = len(empty_tally.requests)
    result = await vouchers.create_receipt(
        services.tools, "Acme Industries", "100", voucher_date=date(2026, 4, 15)
    )
    assert not result.ok
    failure = next(r for r in result.validation.failures if r.rule == "edu_date_allowed")
    assert "Educational mode" in failure.message
    assert empty_tally.vouchers == []
    # It was caught by validation, so no import was attempted.
    assert not any(
        b"IMPORTDATA" in request
        for request in empty_tally.requests[requests_before:]
    )


async def test_tally_itself_would_have_refused_that_date(empty_tally):
    """Belt and braces: the fake reproduces Tally's own EDU rejection, so the
    validation rule is a convenience, not the only thing standing there."""
    services = wire(empty_tally)
    await company_tools.bootstrap_company(services.tools, COMPANY)
    await company_tools.seed_chart_of_accounts(services.tools)
    await services.queue.approve("APR-0001", "test")

    from tallyagent_core.idempotency import make_key
    from tallyagent_core.models import Voucher, VoucherLine, VoucherType

    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=date(2026, 4, 15),
        lines=[
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("100")),
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("-100")),
        ],
    )
    result = await services.tools.backend.create_voucher(
        voucher, make_key(COMPANY, voucher), COMPANY
    )
    assert not result.ok
    assert "1st, 2nd and 31st" in "; ".join(result.errors)


async def test_nothing_posts_before_approval(empty_tally):
    services = wire(empty_tally)
    await company_tools.bootstrap_company(services.tools, COMPANY)

    await company_tools.seed_chart_of_accounts(services.tools)
    assert empty_tally.ledgers == {}, "masters must wait for approval too"
    await services.queue.approve("APR-0001", "test")
    assert empty_tally.ledgers

    await company_tools.seed_demo_transactions(services.tools, 2)
    assert empty_tally.vouchers == [], "vouchers must wait for approval"
    for item in services.queue.list("pending"):
        await services.queue.approve(item.ticket, "test")
    assert empty_tally.vouchers


async def test_the_plain_ledger_collection_returns_names_only(empty_tally):
    """The divergence that made the fake dangerous, now reproduced."""
    services = wire(empty_tally)
    await company_tools.bootstrap_company(services.tools, COMPANY)
    await company_tools.seed_chart_of_accounts(services.tools)
    await services.queue.approve("APR-0001", "test")

    client = services.tools.backend.client
    plain = await client.export_collection("List of Ledgers", "LEDGER", company=COMPANY)
    assert plain, "the collection still lists the ledgers"
    assert all(set(row) <= {"@NAME"} for row in plain), (
        "real Tally returns names only; the fake must not be more generous"
    )

    # Asking properly, with a FETCH, returns the fields.
    masters = await services.tools.masters(refresh=True)
    acme = next(p for p in masters.parties if p.name == "Acme Industries")
    assert acme.state_code == "27"
    assert acme.is_customer
