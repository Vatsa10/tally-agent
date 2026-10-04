"""The morning run where people meet it: the TUI, the web inbox, the daemon's
clock, and the command line.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tallyagent_approvals.people import CLERK, PARTNER
from tallyagent_channels.tui.session import Session
from tallyagent_daemon import clients as clients_mod
from tallyagent_daemon import config as config_mod
from tallyagent_daemon.firm.inbox import Inbox
from tallyagent_daemon.firm.jobs import EXCEPTION, Outcome
from tallyagent_daemon.firm.setup import make_runner, single_client_register

DAY = date(2026, 7, 15)


class OneLine:
    name = "check"

    def due(self, client, today):  # type: ignore[no-untyped-def]
        return True

    async def run(self, ctx, client, today):  # type: ignore[no-untyped-def]
        return [Outcome(job="check", kind=EXCEPTION, title="ITC at risk",
                        amount_at_risk=Decimal("720"), subject="itc")]


@pytest.fixture
def firm(tmp_path):  # type: ignore[no-untyped-def]
    register = clients_mod.parse({
        "storage": {"data_dir": str(tmp_path / "data")},
        "client": [{"slug": "sharma", "company": "Sharma Textiles"},
                   {"slug": "gupta", "company": "Gupta Transport"}],
    })
    base = config_mod.from_dict({"storage": {"firm_db_path": str(tmp_path / "firm.db")}})
    runner = make_runner(base, register, fake=True)
    runner.jobs = [OneLine()]
    return base, register, runner


# --- the TUI -----------------------------------------------------------------


async def test_run_from_the_tui_reports_every_client(firm):
    base, register, runner = firm
    services = runner.wire(register.clients[0]).services
    session = Session(services, firm=runner)

    turn = await session.handle("/run")

    assert "2 client(s)" in turn.text
    assert "sharma" in turn.text and "gupta" in turn.text


async def test_the_inbox_in_the_tui_puts_the_money_first(firm):
    base, register, runner = firm
    session = Session(runner.wire(register.clients[0]).services, firm=runner)
    await session.handle("/run")

    turn = await session.handle("/inbox")

    assert "ITC at risk" in turn.text
    assert "Rs 720.00" in turn.text


async def test_autonomy_in_the_tui_is_a_partners_grant(firm):
    base, register, runner = firm
    services = runner.wire(register.clients[0]).services
    services.people.add("R. Mehta", PARTNER, "4821")
    services.people.add("Nikhil", CLERK, "1199")
    session = Session(services, firm=runner)

    await session.handle("/signin Nikhil 1199")
    refused = await session.handle("/autonomy grant 10 25000")
    assert refused.lines[-1].kind == "error"
    assert services.autonomy.active("Sharma Textiles") is None

    await session.handle("/signin R. Mehta 4821")
    granted = await session.handle("/autonomy grant 10 25000")
    assert "after 10 clean approvals" in granted.text
    assert services.autonomy.active("Sharma Textiles").granted_by == "R. Mehta"

    status = await session.handle("/autonomy")
    assert "on (streak 10" in status.text


async def test_a_session_without_a_firm_run_says_so(firm):
    base, register, runner = firm
    session = Session(runner.wire(register.clients[0]).services)

    assert (await session.handle("/run")).lines[-1].kind == "error"


# --- the daemon's clock ------------------------------------------------------


async def test_the_morning_run_fires_once_a_day_from_the_set_time(firm, tmp_path, monkeypatch):
    from tallyagent_daemon.scheduler import Scheduler

    monkeypatch.chdir(tmp_path)
    base, register, runner = firm
    wired = runner.wire(register.clients[0])
    scheduler = Scheduler(wired, firm=runner, firm_at=(7, 30))

    assert not scheduler.due_firm_run(datetime(2026, 7, 15, 7, 0))
    assert scheduler.due_firm_run(datetime(2026, 7, 15, 7, 30))

    run = await scheduler.run_firm(datetime(2026, 7, 15, 7, 31))
    assert run.ok and "2 client(s)" in run.summary
    assert not scheduler.due_firm_run(datetime(2026, 7, 15, 9, 0)), "once a day"
    assert scheduler.due_firm_run(datetime(2026, 7, 16, 7, 45))
    assert (tmp_path / "reports" / "brief" / "2026-07-15.md").exists()


def test_the_run_time_is_set_in_config():
    config = config_mod.from_dict({"firm": {"run_at": "06:45"}})

    assert config.firm.hour_minute == (6, 45)


# --- the web inbox -----------------------------------------------------------


async def test_the_web_shows_the_firm_inbox_and_a_named_person_resolves_a_line(firm):
    from tallyagent_channels.web.app import build_router

    base, register, runner = firm
    services = runner.wire(register.clients[0]).services
    services.people.add("R. Mehta", PARTNER, "4821")
    await runner.run(DAY)
    app = FastAPI()
    app.include_router(build_router(services, inbox=runner.inbox))
    client = TestClient(app)

    page = client.get("/firm/inbox").text
    assert "ITC at risk" in page and "Rs 720.00" in page

    item = runner.inbox.open()[0]
    wrong = client.post(f"/firm/inbox/{item.id}/resolve", data={"who": "R. Mehta", "pin": "0000"})
    assert "do not match" in wrong.text
    assert item.id in {i.id for i in runner.inbox.open()}

    client.post(f"/firm/inbox/{item.id}/resolve", data={"who": "R. Mehta", "pin": "4821"})
    assert item.id not in {i.id for i in runner.inbox.open()}


# --- an install with no register ---------------------------------------------


def test_a_single_company_install_gets_a_register_of_one():
    """A solo CA with one client should not have to write a register to get a
    morning brief."""
    base = config_mod.from_dict({"tally": {"company": "TA-Demo Traders", "host": "127.0.0.1"}})

    register = single_client_register(base)

    assert [c.company for c in register.clients] == ["TA-Demo Traders"]
    assert register.clients[0].slug == "ta-demo-traders"
    assert register.clients[0].db_path == base.db_path, "the same books it always used"


def test_the_inbox_lives_in_the_firm_database(firm, tmp_path):
    base, register, runner = firm

    assert isinstance(runner.inbox, Inbox)
    assert str(runner.inbox.engine.url).endswith("firm.db")

