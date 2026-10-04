"""The morning run across a practice's clients.

What is being protected: one client's failure never stops the run, every
outcome lands in one inbox ranked by what is at stake, and running twice in a
morning does not double anything.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_approvals.db import make_engine
from tallyagent_daemon import clients as clients_mod
from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_daemon.firm import brief
from tallyagent_daemon.firm.inbox import Inbox
from tallyagent_daemon.firm.jobs import DONE, EXCEPTION, QUEUED, Outcome, previous_month
from tallyagent_daemon.firm.runner import FirmRunner
from tallyagent_tally.fake_server import seeded_demo

DAY = date(2026, 7, 3)


class StubJob:
    """A job that reports what it was told to, so the runner can be tested
    without depending on what any real job finds."""

    def __init__(self, name: str, outcomes: list[Outcome], due: bool = True) -> None:
        self.name = name
        self._outcomes = outcomes
        self._due = due
        self.ran_for: list[str] = []

    def due(self, client, today) -> bool:  # type: ignore[no-untyped-def]
        return self._due

    async def run(self, ctx, client, today):  # type: ignore[no-untyped-def]
        self.ran_for.append(client.slug)
        return [_copy(o) for o in self._outcomes]


def _copy(o: Outcome) -> Outcome:
    return Outcome(
        job=o.job, kind=o.kind, title=o.title, detail=o.detail,
        amount_at_risk=o.amount_at_risk, due=o.due, ticket=o.ticket, subject=o.subject,
    )


class Broken:
    name = "broken"

    def due(self, client, today) -> bool:  # type: ignore[no-untyped-def]
        return True

    async def run(self, ctx, client, today):  # type: ignore[no-untyped-def]
        raise RuntimeError("the reconciliation file was missing")


@pytest.fixture
def practice(tmp_path):  # type: ignore[no-untyped-def]
    """Two clients whose Tally answers, and one whose Tally is off."""
    register = clients_mod.parse(
        {
            "storage": {"data_dir": str(tmp_path / "data")},
            "client": [
                {"slug": "sharma", "company": "Sharma Textiles"},
                {"slug": "gupta", "company": "Gupta Transport"},
                {"slug": "offline", "company": "Offline Traders"},
            ],
        }
    )
    base = config_mod.from_dict({"storage": {"firm_db_path": str(tmp_path / "firm.db")}})
    talliers = {"sharma": seeded_demo("Sharma Textiles"), "gupta": seeded_demo("Gupta Transport")}

    def wire(client):  # type: ignore[no-untyped-def]
        config = clients_mod.apply(base, client, register.data_dir)
        tally = talliers.get(client.slug)
        if tally is None:
            import httpx

            def refuse(request):  # type: ignore[no-untyped-def]
                raise httpx.ConnectError("connection refused")

            return wiring.build(config, transport=httpx.MockTransport(refuse))
        return wiring.build(config, transport=tally.transport)

    inbox = Inbox(make_engine(str(tmp_path / "firm.db")))
    return register, wire, inbox


async def test_every_client_is_run_and_a_dead_one_does_not_stop_the_rest(practice):
    register, wire, inbox = practice
    job = StubJob("check", [Outcome(job="check", kind=DONE, title="all fine")])

    report = await FirmRunner(register, wire, inbox, jobs=[job]).run(DAY)

    assert job.ran_for == ["sharma", "gupta"], "offline skipped, not fatal"
    offline = next(c for c in report.clients if c.slug == "offline")
    assert not offline.healthy
    assert "not answering" in offline.reason
    assert any(o.job == "health" and o.kind == EXCEPTION for o in offline.outcomes)


async def test_a_job_that_throws_is_an_exception_line_not_a_crash(practice):
    register, wire, inbox = practice
    after = StubJob("after", [Outcome(job="after", kind=DONE, title="still ran")])

    runner = FirmRunner(register, wire, inbox, jobs=[Broken(), after])
    report = await runner.run(DAY, only=["sharma"])

    titles = [o.title for o in report.outcomes]
    assert "broken could not run" in titles
    assert "still ran" in titles, "the next job still runs"


async def test_a_client_whose_company_is_not_loaded_is_reported(practice, tmp_path):
    register, wire, inbox = practice
    register.clients[0] = clients_mod.Client(slug="sharma", name="S", company="Not Open Ltd")

    report = await FirmRunner(register, wire, inbox, jobs=[]).run(DAY, only=["sharma"])

    assert "is not loaded" in report.clients[0].reason


async def test_jobs_that_are_not_due_do_not_run_unless_forced(practice):
    register, wire, inbox = practice
    job = StubJob("monthly", [Outcome(job="monthly", kind=DONE, title="x")], due=False)

    await FirmRunner(register, wire, inbox, jobs=[job]).run(DAY, only=["sharma"])
    assert job.ran_for == []

    await FirmRunner(register, wire, inbox, jobs=[job]).run(DAY, only=["sharma"], force=True)
    assert job.ran_for == ["sharma"]


# --- the inbox ---------------------------------------------------------------


async def test_the_inbox_puts_what_needs_a_person_first_and_the_biggest_money_first(practice):
    register, wire, inbox = practice
    job = StubJob(
        "mix",
        [
            Outcome(job="mix", kind=DONE, title="done thing", subject="a"),
            Outcome(job="mix", kind=EXCEPTION, title="small",
                    amount_at_risk=Decimal("10"), subject="b"),
            Outcome(job="mix", kind=EXCEPTION, title="big",
                    amount_at_risk=Decimal("9000"), subject="c"),
            Outcome(job="mix", kind=QUEUED, title="waiting", subject="d"),
        ],
    )

    await FirmRunner(register, wire, inbox, jobs=[job]).run(DAY, only=["sharma"])

    titles = [i.title for i in inbox.open()]
    assert titles[:4] == ["big", "small", "waiting", "done thing"]


async def test_running_twice_in_a_morning_does_not_double_the_inbox(practice):
    register, wire, inbox = practice
    job = StubJob("check", [Outcome(job="check", kind=EXCEPTION, title="same", subject="s")])
    runner = FirmRunner(register, wire, inbox, jobs=[job])

    await runner.run(DAY, only=["sharma"])
    await runner.run(DAY, only=["sharma"])

    assert len([i for i in inbox.open() if i.title == "same"]) == 1


async def test_a_resolved_line_stays_resolved_when_the_run_repeats(practice):
    register, wire, inbox = practice
    job = StubJob("check", [Outcome(job="check", kind=EXCEPTION, title="dealt with", subject="s")])
    runner = FirmRunner(register, wire, inbox, jobs=[job])
    await runner.run(DAY, only=["sharma"])
    item = next(i for i in inbox.open() if i.title == "dealt with")

    inbox.resolve(item.id, by="R. Mehta")
    await runner.run(DAY, only=["sharma"])

    assert not [i for i in inbox.open() if i.title == "dealt with"]


async def test_the_inbox_can_be_read_for_one_client(practice):
    register, wire, inbox = practice
    job = StubJob("check", [Outcome(job="check", kind=EXCEPTION, title="x", subject="s")])
    await FirmRunner(register, wire, inbox, jobs=[job]).run(DAY)

    assert {i.client for i in inbox.open(client="gupta") if i.job == "check"} == {"gupta"}


# --- the brief ---------------------------------------------------------------


async def test_the_brief_has_a_line_per_client_and_the_worst_first(practice, tmp_path):
    register, wire, inbox = practice
    job = StubJob(
        "mix",
        [Outcome(job="mix", kind=EXCEPTION, title="ITC at risk",
                 amount_at_risk=Decimal("12000"), subject="x")],
    )
    report = await FirmRunner(register, wire, inbox, jobs=[job]).run(DAY)

    text = brief.markdown(report)

    assert "| sharma |" in text and "| gupta |" in text
    assert "offline (could not run)" in text
    assert "ITC at risk - Rs 12,000.00" in text
    assert brief.write(report, tmp_path / "brief").read_text(encoding="utf-8") == text
    assert "Could not work on: offline" in brief.whatsapp(report)


# --- the real jobs -----------------------------------------------------------


def test_closing_in_july_means_june():
    assert previous_month(date(2026, 7, 3)) == "2026-06"
    assert previous_month(date(2027, 1, 2)) == "2026-12"


async def test_the_real_jobs_run_against_a_fake_client(practice, tmp_path, monkeypatch):
    """The month-end and GSTR-1 jobs, end to end against the fake Tally."""
    from tallyagent_daemon.firm.jobs import Gstr1Job, MonthEndJob

    monkeypatch.chdir(tmp_path)
    register, wire, inbox = practice

    report = await FirmRunner(
        register, wire, inbox, jobs=[MonthEndJob(), Gstr1Job()]
    ).run(DAY, only=["sharma"])

    jobs = {o.job for o in report.outcomes}
    assert {"month_end", "gstr1"} <= jobs, [o.title for o in report.outcomes]
    assert any("Close pack for 2026-06" in o.title for o in report.outcomes)
