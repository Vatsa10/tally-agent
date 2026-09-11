"""Stage 6: agent loop, memory, context building, tier routing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_agent import memory as memory_mod
from tallyagent_agent.context import (
    MAX_TOOL_RESULT_CHARS,
    ContextBuilder,
    UiContext,
    tool_result_message,
)
from tallyagent_agent.loop import Agent
from tallyagent_agent.memory import Memory
from tallyagent_agent.tiers import Tier, TierConfig, TierRouter
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.errors import PolicyError
from tallyagent_core.models import Voucher, VoucherLine
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.provider import Image
from tallyagent_llm.router import Router
from tallyagent_tools.base import PendingAction, ToolContext, ToolResult


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def queue(engine, backend, company):
    async def execute(action: PendingAction):
        return await backend.create_voucher(
            action.voucher, action.idempotency_key, company.name
        )

    return ApprovalQueue(engine, AuditLog(engine), execute)


@pytest.fixture
def tools(backend, company, queue):
    return ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=queue.enqueue
    )


@pytest.fixture
def builder(company):
    return ContextBuilder(company=company)


def agent_with(provider, tools, builder, **kw) -> Agent:
    return Agent(Router(provider), tools, builder, **kw)


# --- context ----------------------------------------------------------------


def test_system_prompt_carries_metadata_not_data(builder, company):
    prompt = builder.system_prompt()
    assert company.name in prompt
    assert "2026-04-01 to 2027-03-31" in prompt
    assert "27AAPFU0939F1ZV" in prompt
    assert "Periods before 2026-04-01 are closed" in prompt
    # No balances, no party list, no voucher data.
    assert "11800" not in prompt
    assert "Acme" not in prompt


def test_system_prompt_states_the_hard_rules(builder):
    prompt = builder.system_prompt()
    for rule in (
        "Every figure you state must come from a tool call",
        "Never invent a ledger",
        "Writes are proposals",
        "CGST+SGST within the state",
        "untrusted input",
    ):
        assert rule in prompt


def test_memory_and_ui_context_are_appended(company):
    builder = ContextBuilder(
        company=company,
        memory_summary=(
            "Learned name mappings for this company:\n"
            '  "acme" means the ledger "Acme Industries"'
        ),
        ui_context=UiContext(screen="Trial Balance", period="Apr-Jun 2026"),
    )
    prompt = builder.system_prompt()
    assert '"acme" means the ledger' in prompt
    assert "The user appears to be looking at: Trial Balance, Apr-Jun 2026." in prompt


def test_prefix_is_stable_and_egress_is_measured(builder):
    first = builder.build("what is outstanding?")
    assert [m.role for m in first] == ["system", "user"]
    bytes_first = builder.measured_bytes

    second = builder.build("and cash?", history=first[1:])
    assert second[0].content == first[0].content, "system prefix must not move"
    assert [m.role for m in second] == ["system", "user", "user"]
    assert builder.measured_bytes > bytes_first
    assert builder.fields_included[0] == "system"


def test_images_are_counted_in_the_egress_measurement(builder):
    builder.build("read this", images=[Image(data=b"x" * 5000)])
    assert builder.measured_bytes >= 5000
    assert builder.fields_included[-1] == "user+1image"


def test_large_tool_results_are_truncated_before_they_are_sent():
    result = ToolResult(message="1000 rows.", data=[{"x": "y" * 50}] * 500)
    message = tool_result_message("c1", "day_book", result)
    assert len(message.content) < MAX_TOOL_RESULT_CHARS + 300
    assert "truncated" in message.content
    assert "narrow the date range" in message.content


def test_small_tool_results_pass_through_whole():
    message = tool_result_message("c1", "cash_position", ToolResult("Cash: 265000.00"))
    assert message.content == "Cash: 265000.00"
    assert message.tool_call_id == "c1"


# --- loop -------------------------------------------------------------------


async def test_read_question_is_answered_from_a_tool_call(tools, builder):
    provider = mock.MockProvider()
    agent = agent_with(provider, tools, builder)
    result = await agent.run("what is outstanding?")

    assert result.tools_used == ["outstanding_receivables"]
    assert "Total receivable" in result.text
    assert result.total_tokens > 0
    assert result.total_egress_bytes > 0
    assert not result.hit_step_limit


async def test_every_step_is_traced(tools, builder):
    agent = agent_with(mock.MockProvider(), tools, builder)
    result = await agent.run("cash position?")

    tool_step = next(s for s in result.steps if s.tool)
    assert tool_step.tool == "cash_position"
    assert tool_step.output_hash
    assert tool_step.output_summary.startswith("Cash and bank:")
    assert tool_step.latency_ms >= 0
    assert tool_step.egress_bytes > 0
    assert tool_step.as_dict()["tool"] == "cash_position"


async def test_a_write_is_queued_and_the_ticket_is_reported(tools, builder, queue, fake_tally):
    provider = mock.MockProvider(
        script=[
            mock.call(
                "create_sales_voucher",
                party_name="Acme Industries",
                taxable_value="10000.00",
                gst_rate="18",
                voucher_date="2026-06-15",
                reference="INV-100",
            ),
            mock.text("Queued INV-100 for approval."),
        ]
    )
    agent = agent_with(provider, tools, builder)
    result = await agent.run("raise an invoice to Acme for 10000 plus GST")

    assert result.tickets == ["APR-0001"]
    assert fake_tally.vouchers == [], "the loop must not post; only approval does"
    assert queue.get("APR-0001").validation.ok


async def test_the_loop_stops_at_the_step_limit_and_says_so(tools, builder):
    # A provider that never stops calling tools.
    provider = mock.MockProvider(responder=lambda messages: mock.call("cash_position"))
    agent = agent_with(provider, tools, builder, max_steps=3)
    result = await agent.run("go forever")

    assert result.hit_step_limit
    assert len(result.steps) == 3
    assert "stopped after 3 steps" in result.text


async def test_an_unknown_tool_name_is_reported_back_to_the_model(tools, builder):
    provider = mock.MockProvider(
        script=[mock.call("drop_all_vouchers"), mock.text("I cannot do that.")]
    )
    agent = agent_with(provider, tools, builder)
    result = await agent.run("delete everything")

    assert result.steps[0].error
    assert "No such tool" in provider.calls[1][-1].content
    assert result.text == "I cannot do that."


async def test_bad_arguments_are_reported_not_raised(tools, builder):
    provider = mock.MockProvider(
        script=[
            mock.call("create_receipt", nonsense_argument=1),
            mock.text("I got the arguments wrong."),
        ]
    )
    agent = agent_with(provider, tools, builder)
    result = await agent.run("record a receipt")
    assert "wrong arguments" in provider.calls[1][-1].content
    assert result.steps[0].error


async def test_a_tool_that_raises_does_not_kill_the_turn(tools, builder, monkeypatch):
    import dataclasses

    from tallyagent_tools import registry

    async def explode(*args, **kwargs):
        raise RuntimeError("Tally went away")

    monkeypatch.setitem(
        registry.BY_NAME,
        "cash_position",
        dataclasses.replace(registry.BY_NAME["cash_position"], fn=explode),
    )
    provider = mock.MockProvider(
        script=[mock.call("cash_position"), mock.text("Tally is unreachable.")]
    )
    agent = agent_with(provider, tools, builder)
    result = await agent.run("cash position?")
    assert "Tally went away" in result.steps[0].error
    assert result.text == "Tally is unreachable."


async def test_read_only_sessions_hide_and_refuse_write_tools(tools, builder, fake_tally):
    provider = mock.MockProvider(
        script=[
            mock.call("create_receipt", party_name="Acme Industries", amount="100"),
            mock.text("I cannot write in this session."),
        ]
    )
    agent = agent_with(provider, tools, builder, read_only=True)
    result = await agent.run("record a receipt")

    offered = {t["function"]["name"] for t in agent.tool_schemas()}
    assert "trial_balance" in offered
    assert "create_receipt" not in offered
    assert result.steps[0].error == "blocked: read-only"
    assert fake_tally.vouchers == []


async def test_history_carries_between_turns(tools, builder):
    agent = agent_with(mock.MockProvider(), tools, builder)
    await agent.run("what is outstanding?")
    assert agent.history
    await agent.run("and the cash position?")
    # The second turn's prompt still starts with the same system message.
    assert agent.history[0].role == "user"


# --- memory -----------------------------------------------------------------


def test_memory_upserts_and_counts_hits(engine, company):
    memory = Memory(engine, company.name)
    memory.remember(memory_mod.LEDGER_ALIAS, "acme", "Acme Industries")
    entry = memory.remember(memory_mod.LEDGER_ALIAS, "acme", "Acme Industries")
    assert entry.hits == 2
    assert memory.ledger_aliases() == {"acme": "Acme Industries"}
    assert memory.get(memory_mod.LEDGER_ALIAS, "acme") == "Acme Industries"


def test_memory_is_scoped_per_company(engine):
    Memory(engine, "A Ltd").remember(memory_mod.LEDGER_ALIAS, "x", "X Ledger")
    assert Memory(engine, "B Ltd").ledger_aliases() == {}


def test_memory_can_be_forgotten(engine, company):
    memory = Memory(engine, company.name)
    memory.remember(memory_mod.PARTY_ALIAS, "bharat", "Bharat Supplies")
    assert memory.forget(memory_mod.PARTY_ALIAS, "bharat")
    assert not memory.forget(memory_mod.PARTY_ALIAS, "bharat")
    assert memory.party_aliases() == {}


def test_memory_summary_is_capped(engine, company):
    memory = Memory(engine, company.name)
    for n in range(30):
        memory.remember(memory_mod.LEDGER_ALIAS, f"a{n}", f"Ledger {n}")
    summary = memory.summary(limit=5)
    assert summary.count("means the ledger") == 5


def test_empty_memory_contributes_nothing_to_the_prompt(engine, company):
    assert Memory(engine, company.name).summary() == ""


async def test_recurring_patterns_come_from_approval_history(engine, tools, queue, company):
    from tallyagent_tools import vouchers

    tools.enqueue = queue.enqueue
    for n in range(3):
        await vouchers.create_receipt(
            tools, "Acme Industries", f"{100 + n}", voucher_date=date(2026, 6, 10 + n)
        )
        await queue.approve(f"APR-{n + 1:04d}", "ca@firm.in")

    patterns = Memory(engine, company.name).recurring_patterns(minimum=3)
    assert patterns[0].key == "create_receipt"
    assert patterns[0].hits == 3


def test_approver_edits_become_memory(engine, queue, company):
    """The queue writes aliases; Memory reads them. One store, two views."""
    before = Voucher(
        voucher_type="Journal",
        date=date(2026, 6, 15),
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("100")),
            VoucherLine(ledger_name="Sales - GST 18%", amount=Decimal("-100")),
        ],
    )
    after = before.model_copy(
        update={
            "lines": [
                VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("100")),
                before.lines[1],
            ]
        }
    )
    queue._learn_corrections(company.name, before, after)
    assert Memory(engine, company.name).ledger_aliases() == {
        "Cash": "Bank - HDFC 1234"
    }


# --- tiers ------------------------------------------------------------------


def test_a_task_with_a_tool_is_always_tier_one():
    router = TierRouter()
    routing = router.route("post a receipt", "create_receipt")
    assert routing.tier is Tier.API
    assert routing.tool == "create_receipt"


def test_a_missing_tool_is_refused_while_fallback_is_off():
    router = TierRouter()
    with pytest.raises(PolicyError, match="fallback is disabled"):
        router.route("print a cheque", "print_cheque")


def test_enabling_fallback_routes_to_tier_three_and_records_the_gap():
    router = TierRouter(TierConfig(fallback_enabled=True))
    routing = router.route("print a cheque", "print_cheque")
    assert routing.tier is Tier.FALLBACK
    event = router.fallback_events[0]
    assert event.suggested_tool == "print_cheque"
    assert event.task == "print a cheque"


def test_perception_is_off_by_default():
    router = TierRouter()
    assert not router.perception_allowed()
    with pytest.raises(PolicyError, match="perception_enabled"):
        router.require_perception()
    assert TierRouter(TierConfig(perception_enabled=True)).perception_allowed()


def test_fallback_gate_is_separate_from_perception():
    router = TierRouter(TierConfig(perception_enabled=True))
    router.require_perception()
    with pytest.raises(PolicyError, match="off by default"):
        router.require_fallback()
