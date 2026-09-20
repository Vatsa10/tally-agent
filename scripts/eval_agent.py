"""Score the agent's answers, not just its plumbing.

    uv run python scripts/eval_agent.py                 # against live Tally
    uv run python scripts/eval_agent.py --fake          # against the fake

Runs scenarios/quality.yaml and reports, per turn, how the answer was arrived
at: how many tools it called, whether it called any of them twice, whether it
tried the same write twice, how long it took and how long the answer was.

The assertions catch wrong answers. These numbers catch *bad* answers - the
ones that are technically correct and would still lose a room: six lookups for
a question with one, four paragraphs where a sentence was asked for, two
approval tickets for one sale.

Exits non-zero when an assertion fails or a budget is exceeded, so it can gate
a change the same way the tests do.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from tallyagent_channels.tui.script_runner import RunResult, Scenario, run_scenario
from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_demo import env as env_mod

SCENARIO = "scenarios/quality.yaml"

#: What a good answer costs. Deliberately generous - these are meant to catch a
#: regression, not to win an argument about a second either way.
MAX_TOOLS_PER_TURN = 6
MAX_SECONDS_PER_TURN = 25.0
MAX_ANSWER_WORDS = 160


def scorecard(result: RunResult) -> tuple[str, list[str]]:
    """The table, and anything that broke a budget."""
    problems: list[str] = []
    lines = [
        f"{'':3} {'tools':>5} {'rpt':>4} {'secs':>6} {'words':>6}  question",
        "-" * 78,
    ]
    for index, step in enumerate(result.steps, start=1):
        quality = step.quality
        mark = "ok " if step.ok else "FAIL"
        lines.append(
            f"{mark:3} {quality.tool_calls:5} {quality.repeated_tools:4} "
            f"{quality.seconds:6.1f} {quality.answer_words:6}  "
            f"{step.step.say.strip()[:44]}"
        )
        if step.quality.tools:
            lines.append(f"{'':21}  {', '.join(quality.tools)}")

        if not step.ok:
            problems.append(
                f"{index}. {step.step.say.strip()[:50]!r}: "
                + (f"missing {step.missing} " if step.missing else "")
                + (f"forbidden {step.forbidden}" if step.forbidden else "")
            )
        if quality.repeated_writes:
            problems.append(
                f"{index}. wrote twice in one turn: "
                f"{', '.join(quality.repeated_writes)} - one request, two tickets"
            )
        if quality.tool_calls > MAX_TOOLS_PER_TURN:
            problems.append(
                f"{index}. {quality.tool_calls} tool calls for one question "
                f"(budget {MAX_TOOLS_PER_TURN})"
            )
        if quality.seconds > MAX_SECONDS_PER_TURN:
            problems.append(
                f"{index}. took {quality.seconds:.0f}s "
                f"(budget {MAX_SECONDS_PER_TURN:.0f}s)"
            )
        if quality.answer_words > MAX_ANSWER_WORDS:
            problems.append(
                f"{index}. answered in {quality.answer_words} words "
                f"(budget {MAX_ANSWER_WORDS})"
            )

    totals = [s.quality for s in result.steps]
    lines += [
        "-" * 78,
        f"{'':3} {sum(q.tool_calls for q in totals):5} "
        f"{sum(q.repeated_tools for q in totals):4} "
        f"{sum(q.seconds for q in totals):6.1f} "
        f"{sum(q.answer_words for q in totals):6}  totals",
    ]
    return "\n".join(lines), problems


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default=SCENARIO)
    parser.add_argument("--fake", action="store_true", help="use the fake Tally")
    parser.add_argument("--report", default="", help="write the transcript here")
    args = parser.parse_args()

    # Without the key this measures the deterministic mock, which is a fine
    # thing to test the plumbing with and useless for judging answers.
    env_mod.load()

    config = config_mod.load("config/config.toml", "config/policy.toml")
    transport = None
    if args.fake:
        from tallyagent_tally.fake_server import FakeTallyServer, seeded_demo

        server: FakeTallyServer = seeded_demo(config.company.name)
        transport = server.transport()

    wired = wiring.build(config, transport=transport)
    print(f"model    {wired.services.router.name}/{wired.services.router.model}")
    print(f"company  {config.company.name}")
    if wired.using_mock_model:
        print(
            "  (no API key: these answers are the deterministic mock's, and "
            "measuring them says nothing about the real agent)",
            file=sys.stderr,
        )
        return 2

    scenario = Scenario.load(args.scenario)
    result = await run_scenario(
        wired.services, scenario, live=config.live, auto_approve=False
    )

    table, problems = scorecard(result)
    print()
    print(table)
    print()
    print(result.summary)

    if args.report:
        Path(args.report).write_text(result.report(), encoding="utf-8")
        print(f"transcript: {args.report}")

    if problems:
        print("\nproblems:")
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\nEvery answer within budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
