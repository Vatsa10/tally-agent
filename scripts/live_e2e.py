"""Run the end-to-end scenario against a live TallyPrime, with real approvals.

    uv run python scripts/live_e2e.py                    # approve each write yourself
    uv run python scripts/live_e2e.py --auto-approve     # unattended
    uv run python scripts/live_e2e.py --scenario other.yaml

Writes reports/live_e2e_<timestamp>.md summarising every step, and refuses to
start unless the target is inside the live-mode write scope. It is the same
scenario file, and the same Session, that CI runs against the fake.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = ROOT / "scenarios" / "e2e_edu_bootstrap.yaml"
REPORTS = ROOT / "reports"


def _ask(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.toml")
    parser.add_argument("--policy", default="config/policy.toml")
    parser.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Approve every queued write without asking. Unattended runs only.",
    )
    args = parser.parse_args()

    from tallyagent_channels.tui.script_runner import Scenario, run_scenario
    from tallyagent_daemon import config as config_mod
    from tallyagent_daemon import wiring
    from tallyagent_tally.probe import probe

    config = config_mod.load(args.config, args.policy)

    if not config.is_live:
        print(
            f"{args.config} is not in live mode. Set mode = \"live\" and point "
            "[tally] host at your Tally, or run the fake instead:\n"
            "  uv run pytest tests/e2e -q",
            file=sys.stderr,
        )
        return 2

    wired = wiring.build(config)
    report = await probe(wired.backend.client, config.live)
    print(report.render())
    print()
    if not report.ready:
        print("Tally is not ready. Fix the above, or run: tallyagent enable-server")
        return 1

    outside = [c for c in report.companies_loaded if not config.live.may_write_to(c)]
    if outside:
        # A company we must not touch is loaded. Say so before anything runs.
        print(
            f"Refusing to start: {', '.join(outside)} is loaded and is outside the "
            f"write scope {config.live.write_prefix}*. Close it in Tally first.",
            file=sys.stderr,
        )
        return 2

    scenario = Scenario.load(args.scenario)
    print(f"Scenario: {scenario.name} ({len(scenario.steps)} steps)")
    print(f"Target:   {report.url}  [{config.live.describe()}]")
    if args.auto_approve:
        print("Mode:     AUTO-APPROVE - every queued write will post without asking.")
    else:
        print("Mode:     interactive - you approve each write.")
    print()
    if not args.auto_approve and not _ask("This writes to a real Tally. Continue?"):
        print("Cancelled; nothing was sent.")
        return 0

    def announce(step_result) -> None:  # type: ignore[no-untyped-def]
        mark = "ok  " if step_result.ok else "FAIL"
        print(f"[{mark}] {step_result.step.say}")
        for line in step_result.transcript.splitlines()[1:]:
            print(f"         {line}")
        if step_result.missing:
            print(f"         expected but not found: {step_result.missing}")
        print()

    result = await run_scenario(
        wired.services,
        scenario,
        live=config.live,
        auto_approve=args.auto_approve,
        actor="live_e2e",
        on_step=announce,
    )

    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = REPORTS / f"live_e2e_{stamp}.md"
    body = result.report()
    body += "\n## Environment\n\n```\n" + report.render() + "\n```\n"
    path.write_text(body, encoding="utf-8")

    print(f"{'PASS' if result.ok else 'FAIL'} - report written to {path}")
    if not result.ok:
        for failure in result.failures:
            print(f"  failed: {failure.step.say}  missing={failure.missing}")
    print("\nCheck in Tally: Gateway > Day Book, and Balance Sheet.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
