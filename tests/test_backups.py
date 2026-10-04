"""A backup of the books from today before anything is posted in bulk.

What is being protected: a partner who approves forty tickets at once into a
client's real books has a copy of those books from this morning to go back to.
One ticket at a time stays ungated; five or more in one go does not.
"""

from __future__ import annotations

import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.backups import (
    ATTESTED,
    BULK_THRESHOLD,
    FILE_COPY,
    BackupLog,
    BackupNotPossibleError,
    BackupRequiredError,
)
from tallyagent_approvals.db import BackupRow, make_engine
from tallyagent_approvals.people import CLERK, PARTNER, NotPermittedError, People
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_backends.accounting_backend import WriteResult
from tallyagent_channels.services import Services
from tallyagent_channels.tui.session import Session
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tools.base import PendingAction, ToolContext

COMPANY = "Sharma Textiles"


class Clock:
    """A wall clock the test can move: "today" is the whole question here."""

    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def audit(engine):
    return AuditLog(engine)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def backups(engine, audit, clock, tmp_path):
    return BackupLog(engine, audit, root=tmp_path / "backups", now=clock)


@pytest.fixture
def posted():
    return []


@pytest.fixture
def queue(engine, audit, backups, posted):
    async def executor(action: PendingAction) -> WriteResult:
        posted.append(action.summary)
        return WriteResult(ok=True, voucher_number=str(len(posted)))

    approvals = ApprovalQueue(engine, audit, executor=executor)
    approvals.backups = backups
    return approvals


async def queue_tickets(queue: ApprovalQueue, count: int) -> list[str]:
    return [
        await queue.enqueue(
            PendingAction(
                action_type="create_voucher",
                company=COMPANY,
                summary=f"sale {n}",
                payload={"amount": str(Decimal(100 + n))},
            )
        )
        for n in range(count)
    ]


# --- the gate ----------------------------------------------------------------


async def test_a_handful_of_tickets_posts_without_a_backup(queue, posted):
    tickets = await queue_tickets(queue, BULK_THRESHOLD - 1)
    await queue.approve_many(tickets, "R. Mehta")
    assert len(posted) == BULK_THRESHOLD - 1


async def test_five_tickets_in_one_go_are_refused_without_a_backup(queue, posted):
    tickets = await queue_tickets(queue, BULK_THRESHOLD)
    with pytest.raises(BackupRequiredError, match="backup of its books from today"):
        await queue.approve_many(tickets, "R. Mehta")
    assert posted == []
    assert all(queue.get(t).status == "pending" for t in tickets)


async def test_the_refusal_says_exactly_how_to_take_a_backup(queue):
    tickets = await queue_tickets(queue, BULK_THRESHOLD)
    with pytest.raises(BackupRequiredError) as refused:
        await queue.approve_many(tickets, "R. Mehta")
    message = str(refused.value)
    assert "Alt+Y" in message
    assert "/backup confirm" in message
    assert "tallyagent backup take --client" in message
    assert "one at a time" in message


async def test_a_backup_from_today_lets_the_batch_through(queue, backups, posted):
    tickets = await queue_tickets(queue, BULK_THRESHOLD + 2)
    backups.attest(COMPANY, "R. Mehta", r"D:\TallyBackup\2026-10-04")
    await queue.approve_many(tickets, "R. Mehta")
    assert len(posted) == BULK_THRESHOLD + 2


async def test_a_backup_from_yesterday_does_not_count(queue, backups, clock, posted):
    tickets = await queue_tickets(queue, BULK_THRESHOLD)
    clock.at -= timedelta(days=1)
    backups.attest(COMPANY, "R. Mehta", r"D:\TallyBackup\yesterday")
    clock.at += timedelta(days=1)
    with pytest.raises(BackupRequiredError, match="The last one was"):
        await queue.approve_many(tickets, "R. Mehta")
    assert posted == []


async def test_another_companys_backup_does_not_count(queue, backups):
    tickets = await queue_tickets(queue, BULK_THRESHOLD)
    backups.attest("Gupta Steels", "R. Mehta", r"D:\TallyBackup\gupta")
    with pytest.raises(BackupRequiredError):
        await queue.approve_many(tickets, "R. Mehta")


# --- recording one -----------------------------------------------------------


def test_an_attestation_carries_the_partners_name_and_where(backups, engine):
    backup = backups.attest(COMPANY, "R. Mehta", r"D:\TallyBackup\Sharma")
    assert backup.method == ATTESTED
    with DbSession(engine) as session:
        row = session.exec(select(BackupRow)).one()
    assert (row.company, row.taken_by, row.path) == (
        COMPANY,
        "R. Mehta",
        r"D:\TallyBackup\Sharma",
    )


def test_an_attestation_without_saying_where_is_refused(backups):
    with pytest.raises(ValueError, match="where the backup is"):
        backups.attest(COMPANY, "R. Mehta", "  ")


def test_every_backup_goes_on_the_audit_chain(backups, audit):
    backups.attest(COMPANY, "R. Mehta", r"D:\TallyBackup\Sharma")
    entry = audit.entries()[-1]
    assert entry.event == "backup_recorded"
    assert entry.actor == "R. Mehta"
    assert entry.payload["company"] == COMPANY
    assert entry.payload["method"] == ATTESTED
    assert audit.verify().ok


def test_a_file_copy_with_no_data_folder_configured_says_what_to_do(backups):
    with pytest.raises(BackupNotPossibleError, match="Alt\\+Y"):
        backups.take(COMPANY, "R. Mehta")


def test_a_file_copy_zips_tallys_data_folder_and_the_firm_database(
    engine, audit, clock, tmp_path
):
    data = tmp_path / "TallyData" / "10000"
    data.mkdir(parents=True)
    (data / "Company.900").write_bytes(b"tally company file")
    (data / "Tranmgr.900").write_bytes(b"tally transactions")
    firm_db = tmp_path / "firm.db"
    with sqlite3.connect(firm_db) as firm:
        firm.execute("create table users (name text)")
        firm.execute("insert into users values ('R. Mehta')")
    firm.close()

    backups = BackupLog(
        engine,
        audit,
        data_dir=str(tmp_path / "TallyData"),
        firm_db_path=str(firm_db),
        root=tmp_path / "backups",
        now=clock,
    )
    backup = backups.take(COMPANY, "R. Mehta")

    archive = Path(backup.path)
    assert backup.method == FILE_COPY
    assert archive.parent.parent == tmp_path / "backups" / "Sharma_Textiles"
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert {"tally/10000/Company.900", "tally/10000/Tranmgr.900"} <= names
        assert zf.read("tally/10000/Company.900") == b"tally company file"
        restored = tmp_path / "restored.db"
        restored.write_bytes(zf.read("firm/firm.db"))
    with sqlite3.connect(restored) as copy:
        assert copy.execute("select name from users").fetchall() == [("R. Mehta",)]
    copy.close()
    assert backups.taken_today(COMPANY) is not None


# --- who may take one, from the chat window ----------------------------------


@pytest.fixture
def session(engine, audit, backups, queue, backend):
    people = People(engine, audit)
    people.add("R. Mehta", PARTNER, "4821")
    people.add("Nikhil", CLERK, "1199")
    from tallyagent_core.models import Company

    company = Company(name=COMPANY, state_code="27")
    tools = ToolContext(
        backend=backend,
        company=company,
        policy=Policy.default(),
        enqueue=queue.enqueue,
        source="tui",
    )
    services = Services(
        company=company,
        tools=tools,
        queue=queue,
        audit=audit,
        router=Router(mock.MockProvider()),
        people=people,
        backups=backups,
    )
    return Session(services)


async def test_a_clerk_cannot_record_a_backup(session, backups):
    await session.handle("/signin Nikhil 1199")
    turn = await session.handle(r"/backup confirm D:\TallyBackup\Sharma")
    assert turn.lines[-1].kind == "error"
    assert "partner" in turn.text
    assert backups.latest(COMPANY) is None


def test_taking_a_backup_is_a_partners_decision():
    from tallyagent_approvals.people import User

    with pytest.raises(NotPermittedError):
        User("Nikhil", CLERK).require("take_backup")


async def test_approve_all_in_the_chat_window_stops_at_the_gate(session, queue, posted):
    await queue_tickets(queue, BULK_THRESHOLD)
    await session.handle("/signin R. Mehta 4821")
    turn = await session.handle("/approve A")
    assert turn.lines[-1].kind == "error"
    assert "/backup confirm" in turn.text
    assert posted == []


async def test_a_partner_records_a_backup_and_then_approve_all_posts(
    session, queue, posted
):
    await queue_tickets(queue, BULK_THRESHOLD)
    await session.handle("/signin R. Mehta 4821")
    recorded = await session.handle(r"/backup confirm D:\TallyBackup\Sharma")
    assert "backup recorded" in recorded.text
    assert "R. Mehta" in recorded.text

    await session.handle("/approve A")
    assert len(posted) == BULK_THRESHOLD
    assert session.pending() == []


async def test_backup_with_nothing_to_copy_explains_how(session):
    await session.handle("/signin R. Mehta 4821")
    turn = await session.handle("/backup")
    assert "No backup on record" in turn.text
    assert "Alt+Y" in turn.text
