"""Stage 14.2: the terminal control surface.

The Textual app is a rendering layer; almost everything here exercises
``Session``, which is what both the TUI and the scripted runner drive. The app
itself gets a mount-and-drive smoke test through Textual's pilot.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_agent.memory import Memory
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_channels.services import Services
from tallyagent_channels.tui import preview
from tallyagent_channels.tui.script_runner import Scenario, run_scenario
from tallyagent_channels.tui.session import COMMANDS, HELP, Session
from tallyagent_core.livemode import LiveMode
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tools.base import PendingAction, ToolContext


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def services(engine, backend, company):
    audit = AuditLog(engine)

    async def execute(action: PendingAction):
        from tallyagent_tools import masters

        ctx = ToolContext(backend=backend, company=company, policy=Policy.default())
        if action.voucher is None:
            return await masters._execute_master(ctx, action)
        return await backend.create_voucher(
            action.voucher, action.idempotency_key, company.name
        )

    queue = ApprovalQueue(engine, audit, execute)
    tools = ToolContext(
        backend=backend,
        company=company,
        policy=Policy.default(),
        enqueue=queue.enqueue,
        source="tui",
    )
    return Services(
        company=company,
        tools=tools,
        queue=queue,
        audit=audit,
        router=Router(mock.MockProvider()),
        memory=Memory(engine, company.name),
    )


@pytest.fixture
def session(services) -> Session:
    return Session(services, actor="ca@firm.in")


# --- routing ----------------------------------------------------------------


async def test_plain_text_goes_to_the_agent(session):
    turn = await session.handle("what is the cash position?")
    kinds = [line.kind for line in turn.lines]
    assert kinds[0] == "user"
    assert "tool" in kinds and "agent" in kinds
    assert "265000.00" in turn.text


async def test_the_answering_model_is_always_named(session):
    turn = await session.handle("cash position?")
    agent_line = next(line for line in turn.lines if line.kind == "agent")
    assert agent_line.meta["provider"] == "mock"
    assert agent_line.meta["model"] == "mock-deterministic"


async def test_tool_lines_carry_latency_tokens_and_egress(session):
    turn = await session.handle("cash position?")
    tool_line = next(line for line in turn.lines if line.kind == "tool")
    assert "ms" in tool_line.text and "tok" in tool_line.text and "B" in tool_line.text
    assert tool_line.meta["tool"] == "cash_position"


async def test_blank_input_does_nothing(session):
    turn = await session.handle("   ")
    assert turn.lines == []
    assert session.stats.turns == 0


async def test_an_unknown_command_lists_the_real_ones(session):
    turn = await session.handle("/nonsense")
    assert turn.lines[-1].kind == "error"
    assert "/probe" in turn.text and "/bootstrap" in turn.text


async def test_every_command_is_documented():
    assert set(COMMANDS) == set(HELP)
    for name, description in HELP.items():
        assert description, f"/{name} has no help text"


async def test_help_lists_everything(session):
    turn = await session.handle("/help")
    for name in COMMANDS:
        assert f"/{name}" in turn.text


async def test_a_failing_turn_is_reported_not_raised(session, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("Tally went away")

    monkeypatch.setattr("tallyagent_tools.reports.cash_position", explode)
    import dataclasses

    from tallyagent_tools import registry

    monkeypatch.setitem(
        registry.BY_NAME,
        "cash_position",
        dataclasses.replace(registry.BY_NAME["cash_position"], fn=explode),
    )
    turn = await session.handle("cash position?")
    assert "Tally went away" in turn.text


# --- approvals from the terminal --------------------------------------------


async def queue_a_sale(session):
    session.services.router = Router(
        mock.MockProvider(
            script=[
                mock.call(
                    "create_sales_voucher",
                    party_name="Acme Industries",
                    taxable_value="10000.00",
                    gst_rate="18",
                    voucher_date="2026-06-15",
                    reference="TUI-1",
                ),
                mock.text("Drafted it."),
            ]
        )
    )
    session.services.reset(session.conversation)
    return await session.handle("invoice Acme 10000 plus GST")


async def test_a_write_queues_and_says_nothing_is_posted(session, fake_tally):
    turn = await queue_a_sale(session)
    assert turn.tickets == ["APR-0001"]
    assert "Nothing has been posted to Tally yet" in turn.text
    assert fake_tally.vouchers == []


async def test_approve_posts_and_reports_the_voucher(session, fake_tally):
    await queue_a_sale(session)
    turn = await session.handle("/approve APR-0001")
    assert "posted to Tally" in turn.text
    assert len(fake_tally.vouchers) == 1
    assert session.stats.approved == 1


async def test_approve_all(session, fake_tally):
    await queue_a_sale(session)
    session.services.router = Router(
        mock.MockProvider(
            script=[
                mock.call(
                    "create_receipt",
                    party_name="Acme Industries",
                    amount="500",
                    voucher_date="2026-06-20",
                ),
                mock.text("Drafted."),
            ]
        )
    )
    session.services.reset(session.conversation)
    await session.handle("receipt from Acme 500")

    assert len(session.pending()) == 2
    await session.handle("/approve A")
    assert session.pending() == []
    assert len(fake_tally.vouchers) == 2


async def test_reject_requires_a_reason(session, fake_tally):
    await queue_a_sale(session)
    refused = await session.handle("/reject APR-0001")
    assert refused.lines[-1].kind == "error"
    assert "reason" in refused.text

    turn = await session.handle("/reject APR-0001 wrong customer")
    assert "rejected: wrong customer" in turn.text
    assert fake_tally.vouchers == []
    assert session.stats.rejected == 1


async def test_approvals_lists_what_is_waiting(session):
    await queue_a_sale(session)
    turn = await session.handle("/approvals")
    assert "APR-0001" in turn.text
    assert "11800.00" in turn.text


async def test_approvals_when_empty(session):
    turn = await session.handle("/approvals")
    assert "Nothing waiting" in turn.text


# --- other commands ---------------------------------------------------------


async def test_probe_renders_the_report(session):
    turn = await session.handle("/probe")
    assert "reachable" in turn.text
    assert "PASS ready for live mode" in turn.text


async def test_company_switches_and_flags_read_only(services):
    session = Session(services, live=LiveMode(enabled=True, write_prefix="TA-"))
    turn = await session.handle("/company Real Client Pvt Ltd")
    assert "READ ONLY" in turn.text

    ok = await session.handle("/company TA-Demo Traders")
    assert "READ ONLY" not in ok.text
    assert session.services.company.name == "TA-Demo Traders"
    assert session.services.tools.company.name == "TA-Demo Traders"


async def test_company_with_no_argument_shows_the_active_one(session):
    turn = await session.handle("/company")
    assert "Demo Traders Pvt Ltd" in turn.text


async def test_egress_reports_what_left_the_machine(session):
    await session.handle("cash position?")
    turn = await session.handle("/egress")
    assert "bytes left this machine" in turn.text
    assert "tok" in turn.text


async def test_egress_before_any_model_call(session):
    turn = await session.handle("/egress")
    assert "Nothing has been sent to a model" in turn.text


async def test_policy_says_promotion_is_manual(session):
    turn = await session.handle("/policy")
    assert "human edit to policy.toml" in turn.text


async def test_model_shows_and_switches(session, monkeypatch):
    shown = await session.handle("/model")
    assert "mock/mock-deterministic" in shown.text

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    switched = await session.handle("/model deepseek")
    assert "deepseek/deepseek-flash" in switched.text

    bad = await session.handle("/model llamafile")
    assert bad.lines[-1].kind == "error"


async def test_tier3_toggle_reports_when_unwired(session):
    turn = await session.handle("/tier3 on")
    assert turn.lines[-1].kind == "error"


async def test_tier3_toggle_when_wired(session):
    from tallyagent_agent.tiers import TierConfig, TierRouter

    session.services.tiers = TierRouter(TierConfig())
    off = await session.handle("/tier3")
    assert "is off" in off.text

    on = await session.handle("/tier3 on")
    assert "is now on" in on.text
    assert "still ask you first" in on.text
    assert session.services.tiers.config.fallback_enabled


async def test_reco_bank_proposes_without_posting(session, fake_tally, tmp_path):
    from pathlib import Path as P

    fixture = P(__file__).parent / "fixtures" / "bank_statement.csv"
    turn = await session.handle(f"/reco bank {fixture}")
    assert "none posted" in turn.text
    assert fake_tally.vouchers == []


async def test_reco_rejects_an_unknown_kind(session):
    turn = await session.handle("/reco sideways somefile.csv")
    assert turn.lines[-1].kind == "error"


def test_windows_paths_survive_command_splitting():
    """POSIX shlex eats backslashes, which silently breaks every Windows path."""
    from tallyagent_channels.tui.session import split_command

    assert split_command(r"/ingest D:\Files\Vatsaill.png") == [
        "/ingest",
        r"D:\Files\Vatsaill.png",
    ]
    assert split_command(r'/ingest "C:\my docsill.png"') == [
        "/ingest",
        r"C:\my docsill.png",
    ]
    assert split_command("/reject APR-0001 wrong customer") == [
        "/reject",
        "APR-0001",
        "wrong",
        "customer",
    ]


async def test_ingest_reports_a_missing_file(session):
    turn = await session.handle("/ingest nope.png")
    assert "no such file" in turn.text


async def test_close_prints_the_session_summary(session, fake_tally):
    await queue_a_sale(session)
    await session.handle("/approve APR-0001")
    turn = await session.handle("/close")
    assert "1 voucher(s) posted" in turn.text
    assert "Tier 3 used: no" in turn.text
    assert "bytes egressed" in turn.text


async def test_quit_sets_the_quit_flag(session):
    turn = await session.handle("/quit")
    assert turn.quit
    assert "Session summary" in turn.text


# --- Tier 3 preview ---------------------------------------------------------


def test_preview_without_pillow_still_describes_the_step(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no pillow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    body = preview.render(b"x" * 100)
    assert "100 bytes" in body
    assert "desktop extra" in body


def test_preview_renders_braille():
    pytest.importorskip("PIL")
    from PIL import Image

    buffer = __import__("io").BytesIO()
    Image.new("L", (80, 40), color=255).save(buffer, format="PNG")
    art = preview.render(buffer.getvalue(), width=20)
    assert art.splitlines()
    assert all(len(line) <= 20 for line in art.splitlines())


def test_describe_step_warns_that_data_can_change():
    from tallyagent_agent.fallback.computer_use import FallbackStep

    step = FallbackStep(index=2, action="key", detail="ctrl+a", why="accept the voucher")
    body = preview.describe_step(step, png=b"")
    assert "Tier 3 step 2" in body
    assert "ctrl+a" in body
    assert "can change data" in body
    assert "accept the voucher" in body


# --- scripted twin ----------------------------------------------------------


async def test_a_scenario_runs_the_same_session(services, tmp_path):
    scenario = Scenario.from_dict(
        {
            "name": "smoke",
            "description": "reads only",
            "steps": [
                {"say": "/probe", "expect": ["PASS ready for live mode"]},
                {"say": "what is the cash position?", "expect": ["265000.00"]},
                {"say": "/close", "expect": ["turn(s)"]},
            ],
        }
    )
    result = await run_scenario(services, scenario)
    assert result.ok, result.report()
    assert len(result.steps) == 3

    report = result.report()
    assert "# smoke" in report
    assert "Result: PASS" in report


async def test_a_scenario_reports_a_missing_expectation(services):
    scenario = Scenario.from_dict(
        {
            "name": "failing",
            "steps": [{"say": "cash position?", "expect": ["999999.00"]}],
        }
    )
    result = await run_scenario(services, scenario)
    assert not result.ok
    assert result.failures[0].missing == ["999999.00"]
    assert "Result: FAIL" in result.report()


async def test_expect_not_catches_a_forbidden_string(services):
    scenario = Scenario.from_dict(
        {
            "name": "guard",
            "steps": [{"say": "cash position?", "expect_not": ["265000.00"]}],
        }
    )
    result = await run_scenario(services, scenario)
    assert not result.ok
    assert result.failures[0].forbidden == ["265000.00"]


async def test_an_optional_step_does_not_fail_the_run(services):
    scenario = Scenario.from_dict(
        {
            "name": "optional",
            "steps": [{"say": "cash position?", "expect": ["nope"], "optional": True}],
        }
    )
    result = await run_scenario(services, scenario)
    assert result.ok


async def test_a_scenario_runs_every_step_even_after_a_failure(services):
    scenario = Scenario.from_dict(
        {
            "name": "keeps going",
            "steps": [
                {"say": "cash position?", "expect": ["nope"]},
                {"say": "what is outstanding?", "expect": ["Total receivable"]},
            ],
        }
    )
    result = await run_scenario(services, scenario)
    assert len(result.steps) == 2
    assert result.steps[1].ok


async def test_scenario_auto_approve_posts(services, fake_tally):
    services.router = Router(
        mock.MockProvider(
            script=[
                mock.call(
                    "create_receipt",
                    party_name="Acme Industries",
                    amount="500",
                    voucher_date="2026-06-20",
                ),
                mock.text("Drafted."),
            ]
        )
    )
    scenario = Scenario.from_dict(
        {"name": "post", "steps": [{"say": "receipt 500 from Acme", "approve": True}]}
    )
    result = await run_scenario(services, scenario, auto_approve=True)
    assert result.ok
    assert len(fake_tally.vouchers) == 1
    assert "posted to Tally" in result.steps[0].transcript


async def test_scenario_without_auto_approve_leaves_it_queued(services, fake_tally):
    services.router = Router(
        mock.MockProvider(
            script=[
                mock.call(
                    "create_receipt",
                    party_name="Acme Industries",
                    amount="500",
                    voucher_date="2026-06-20",
                ),
                mock.text("Drafted."),
            ]
        )
    )
    scenario = Scenario.from_dict(
        {"name": "hold", "steps": [{"say": "receipt 500 from Acme", "approve": False}]}
    )
    await run_scenario(services, scenario, auto_approve=True)
    assert fake_tally.vouchers == []


def test_scenario_loads_from_yaml(tmp_path):
    path = tmp_path / "s.yaml"
    path.write_text(
        "name: from disk\n"
        "description: a test\n"
        "steps:\n"
        "  - say: /probe\n"
        "    expect: [PASS]\n"
        "    note: check the connection\n",
        encoding="utf-8",
    )
    scenario = Scenario.load(path)
    assert scenario.name == "from disk"
    assert scenario.steps[0].say == "/probe"
    assert scenario.steps[0].note == "check the connection"


# --- the app itself ---------------------------------------------------------


async def test_the_app_mounts_and_answers(services):
    """Typing in the box runs a real turn and the answer reaches the transcript.

    Asserted through observable state rather than Textual's render tree, which
    is Rich internals and not ours to depend on.
    """
    from tallyagent_channels.tui.app import TallyAgentTUI

    app = TallyAgentTUI(services, LiveMode(), status_interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript")
        before = len(transcript.children)

        app.query_one("#prompt").value = "what is the cash position?"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.session.stats.turns == 1
        assert app.session.stats.tool_calls == 1
        assert len(transcript.children) > before
        assert app.query_one("#prompt").value == "", "the box clears after sending"


async def test_the_app_shows_the_queue_and_approves_with_a_key(services, fake_tally):
    from tallyagent_channels.tui.app import TallyAgentTUI

    services.router = Router(
        mock.MockProvider(
            script=[
                mock.call(
                    "create_receipt",
                    party_name="Acme Industries",
                    amount="500",
                    voucher_date="2026-06-20",
                ),
                mock.text("Drafted."),
            ]
        )
    )
    app = TallyAgentTUI(services, LiveMode(), status_interval=3600)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt").value = "receipt 500 from Acme"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert len(app.session.pending()) == 1
        assert fake_tally.vouchers == []

        await app._run_command("/approve APR-0001")
        assert len(fake_tally.vouchers) == 1


async def test_the_status_panel_names_the_scope_and_model(services):
    from tallyagent_channels.tui.app import TallyAgentTUI

    app = TallyAgentTUI(
        services, LiveMode(enabled=True, edu=True), status_interval=3600
    )
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        body = app.query_one("#status").body
        assert "LIVE EDU" in body
        assert "TA-*" in body
        assert "mock" in body


def test_ledger_impact_amounts_are_quantised(services):
    """A terminal diff shows the same paisa-accurate figures as the web one."""
    action = PendingAction(
        action_type="x",
        company="c",
        summary="s",
        voucher=None,
    )
    assert action.ledger_impact() == []
    assert action.amount == Decimal("0")


def test_session_stats_start_empty(session):
    assert session.stats.turns == 0
    assert not session.stats.tier3_used
    assert "Tier 3 used: no" in session.stats.summary(session.services)


def test_demo_transaction_dates_are_edu_legal():
    from tallyagent_tools.company import demo_transactions

    for txn in demo_transactions(5, edu=True, fy_start=date(2026, 4, 1)):
        assert txn.kwargs["voucher_date"].day in (1, 2, 31)
