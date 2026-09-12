"""What the agent does twice, and what it says badly.

Both of these were found by watching a real turn against live Tally rather than
by reading the code: the same lookup went out three times in one answer, and a
validation message arrived on screen cut off mid-word.
"""

from __future__ import annotations

from tallyagent_agent.context import ContextBuilder
from tallyagent_agent.loop import SUMMARY_CHARS, Agent, _summarise
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tools.base import ToolContext

# --- summaries --------------------------------------------------------------


def test_a_short_message_is_left_alone():
    assert _summarise("2 stock item(s).") == "2 stock item(s)."


def test_a_long_message_is_never_cut_mid_word():
    """This is the bug: "...an invoice reference to tell th" on screen."""
    message = (
        "looks like a duplicate of voucher 6 dated 2026-06-02: same party, "
        "amount 7552.00 on the same date, and neither carries an invoice "
        "reference to tell them apart, so it is refused until one of them is "
        "given a number that distinguishes it from the other invoice entirely."
    )
    summary = _summarise(message)

    assert len(summary) <= SUMMARY_CHARS + 3
    assert summary.endswith("...")
    assert not summary[:-3].endswith(" ")
    # The last word is a whole word, not a fragment of one.
    assert message.split()[len(summary[:-3].split()) - 1].startswith(
        summary[:-3].split()[-1]
    )


def test_newlines_are_flattened_so_one_summary_is_one_line():
    assert "\n" not in _summarise("first line\nsecond line\n\nthird")


# --- doing the same work twice ----------------------------------------------


def _agent(script, backend, company) -> tuple[Agent, ToolContext]:  # type: ignore[no-untyped-def]
    tools = ToolContext(backend=backend, company=company, policy=Policy.default())
    agent = Agent(
        Router(mock.MockProvider(script=script)),
        tools,
        ContextBuilder(company=company),
        max_steps=8,
    )
    return agent, tools


async def test_the_same_read_twice_in_one_turn_only_asks_tally_once(backend, company):
    agent, _ = _agent(
        [
            mock.call("list_ledgers"),
            mock.call("list_ledgers"),
            mock.text("Done."),
        ],
        backend,
        company,
    )

    result = await agent.run("which ledgers exist?")

    steps = [s for s in result.steps if s.tool == "list_ledgers"]
    assert len(steps) == 2, "the model still asked twice"
    assert steps[0].cached is False
    assert steps[1].cached is True, "the second one should not have gone to Tally"
    assert steps[0].output_summary == steps[1].output_summary


async def test_different_arguments_are_different_questions(backend, company):
    agent, _ = _agent(
        [
            mock.call("day_book", from_date="2026-06-01"),
            mock.call("day_book", from_date="2026-06-02"),
            mock.text("Done."),
        ],
        backend,
        company,
    )

    result = await agent.run("who are these?")

    assert [s.cached for s in result.steps if s.tool] == [False, False]


async def test_a_write_is_never_answered_from_the_memo(backend, company):
    """Two identical writes are two writes. Silently swallowing one is worse
    than doing it twice - the duplicate rule exists to catch that."""
    agent, _ = _agent(
        [
            mock.call("create_receipt", party_name="Acme Industries", amount="500",
                      voucher_date="2026-06-01"),
            mock.call("create_receipt", party_name="Acme Industries", amount="500",
                      voucher_date="2026-06-01"),
            mock.text("Done."),
        ],
        backend,
        company,
    )

    result = await agent.run("receipt 500 from Acme, twice")

    writes = [s for s in result.steps if s.tool == "create_receipt"]
    assert len(writes) == 2
    assert all(not s.cached for s in writes)


async def test_the_memo_does_not_leak_between_turns(backend, company):
    """A second question must see the books as they are now, not as they were."""
    agent, _ = _agent(
        [
            mock.call("list_ledgers"),
            mock.text("First."),
            mock.call("list_ledgers"),
            mock.text("Second."),
        ],
        backend,
        company,
    )

    await agent.run("which ledgers exist?")
    second = await agent.run("and now?")

    assert [s.cached for s in second.steps if s.tool] == [False]


# --- what the terminal does with a table -------------------------------------


def test_a_markdown_table_is_rendered_as_one():
    """A trial balance arrives as pipes and dashes; raw, it is not a report."""
    from tallyagent_channels.tui.app import _is_markdown

    assert _is_markdown("| Ledger | Balance |\n|---|---|\n| Cash | 15,000.00 |")


def test_a_bullet_list_is_rendered_too():
    from tallyagent_channels.tui.app import _is_markdown

    assert _is_markdown("- first thing\n- second thing")


def test_a_plain_sentence_is_left_exactly_as_written():
    """Reflowing every answer would swallow stray asterisks and rupee signs."""
    from tallyagent_channels.tui.app import _is_markdown

    assert not _is_markdown("Queued for approval as APR-0007. Nothing posted yet.")
    assert not _is_markdown("There is no Baroda cost centre.\nThe closest is Vadodara.")
