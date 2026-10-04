"""Named users, two roles, and a PIN at the moment of a decision.

What is being protected: an approval carries the name of somebody who will
stand behind it. Before this the web form asserted its own actor, so every
approval in the audit chain said "web".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.people import (
    CLERK,
    MAX_FAILED_PINS,
    PARTNER,
    LockedOutError,
    NotPermittedError,
    NotSignedInError,
    People,
    User,
    WrongPinError,
    hash_pin,
)


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def audit(engine):
    return AuditLog(engine)


@pytest.fixture
def people(engine, audit):
    staff = People(engine, audit)
    staff.add("R. Mehta", PARTNER, "4821")
    staff.add("Nikhil", CLERK, "1199")
    return staff


# --- the register ------------------------------------------------------------


def test_a_fresh_install_has_nobody(engine):
    assert People(engine).empty


def test_the_same_name_cannot_be_registered_twice(people):
    with pytest.raises(ValueError, match="already registered"):
        people.add("Nikhil", CLERK, "2222")


def test_a_pin_has_to_be_digits_long_enough_to_matter():
    for bad in ("", "12", "abcd", "12a4", "123456789"):
        with pytest.raises(ValueError, match="4 to 8 digits"):
            hash_pin(bad)


def test_the_table_does_not_hold_the_pin(engine):
    from sqlmodel import Session, select

    from tallyagent_approvals.db import UserRow

    People(engine).add("R. Mehta", PARTNER, "4821")

    with Session(engine) as session:
        row = session.exec(select(UserRow)).one()
    assert "4821" not in row.pin_hash
    assert row.pin_salt and row.pin_salt != row.pin_hash


def test_two_people_with_the_same_pin_do_not_share_a_hash(people):
    """A per-user salt, so the table cannot be read as "these two are the same"."""
    people.add("Asha", CLERK, "1199")

    from sqlmodel import Session, select

    from tallyagent_approvals.db import UserRow

    with Session(people.engine) as session:
        rows = {r.name: r.pin_hash for r in session.exec(select(UserRow)).all()}
    assert rows["Nikhil"] != rows["Asha"]


# --- signing in --------------------------------------------------------------


def test_the_right_pin_signs_a_person_in(people):
    user = people.sign_in("R. Mehta", "4821")

    assert user == User("R. Mehta", PARTNER)
    assert people.current == user


def test_a_wrong_pin_signs_nobody_in(people):
    with pytest.raises(WrongPinError):
        people.sign_in("R. Mehta", "0000")

    assert people.current is None


def test_an_unknown_name_and_a_wrong_pin_read_the_same(people):
    """Which names are registered is not something a form tells whoever asks."""
    with pytest.raises(WrongPinError) as unknown:
        people.check("Somebody Else", "4821")
    with pytest.raises(WrongPinError) as wrong:
        people.check("R. Mehta", "0000")

    assert str(unknown.value) == str(wrong.value)


def test_a_disabled_person_cannot_sign_in(people):
    people.disable("Nikhil", by="R. Mehta")

    with pytest.raises(WrongPinError):
        people.sign_in("Nikhil", "1199")
    assert [u.name for u in people.list()] == ["R. Mehta"]


def test_nothing_is_attempted_with_nobody_signed_in(people):
    with pytest.raises(NotSignedInError, match="/signin"):
        people.require("approve")


# --- what each role may do ---------------------------------------------------


def test_a_clerk_does_the_work(people):
    clerk = people.sign_in("Nikhil", "1199")

    for allowed in ("draft", "run_bills", "month_end_close", "read_reports"):
        assert clerk.may(allowed), allowed


def test_a_clerk_cannot_approve(people):
    """The whole design is that the agent types and the firm decides; a junior
    approving their own draft is the one hole that would make it decorative."""
    people.sign_in("Nikhil", "1199")

    with pytest.raises(NotPermittedError, match="partner's decision"):
        people.require("approve")


def test_a_clerk_cannot_enable_a_client_company_either(people):
    people.sign_in("Nikhil", "1199")

    with pytest.raises(NotPermittedError):
        people.require("grant_consent")


def test_a_partner_may_decide(people):
    people.sign_in("R. Mehta", "4821")

    for action in ("approve", "reject", "grant_consent", "enable_keyboard_tier"):
        assert people.require(action).is_partner, action


def test_a_promotion_takes_effect(people):
    people.set_role("Nikhil", PARTNER, by="R. Mehta")

    assert people.sign_in("Nikhil", "1199").is_partner


# --- deciding without a session ----------------------------------------------


def test_the_web_form_proves_the_decision_itself(people):
    """There is no session to trust in an HTMX post, so name plus PIN is checked
    at the moment of the approval."""
    user = people.authorise("approve", "R. Mehta", "4821")

    assert user.name == "R. Mehta"


def test_a_clerks_pin_does_not_approve_a_ticket(people):
    with pytest.raises(NotPermittedError):
        people.authorise("approve", "Nikhil", "1199")


def test_a_wrong_pin_in_the_form_approves_nothing(people):
    with pytest.raises(WrongPinError):
        people.authorise("approve", "R. Mehta", "9999")


# --- the log -----------------------------------------------------------------


def test_sign_ins_and_failures_are_both_recorded(people, audit):
    people.sign_in("R. Mehta", "4821")
    with pytest.raises(WrongPinError):
        people.sign_in("Nikhil", "0000")

    events = [(e.event, e.actor) for e in audit.entries()]

    assert ("signed_in", "R. Mehta") in events
    assert ("signin_failed", "Nikhil") in events
    assert audit.verify().ok


def test_a_failed_log_write_does_not_stop_somebody_signing_in(engine):
    class Broken:
        def append(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("disk full")

    staff = People(engine, Broken())
    staff.add("R. Mehta", PARTNER, "4821")

    assert staff.sign_in("R. Mehta", "4821").is_partner


# --- lockout -----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def timed(engine, audit, clock):
    staff = People(engine, audit, clock=clock)
    staff.add("R. Mehta", PARTNER, "4821")
    return staff


def _miss(staff, times):
    for _ in range(times):
        with pytest.raises(WrongPinError):
            staff.check("R. Mehta", "0000")


def test_five_wrong_pins_in_a_row_lock_the_person_out(timed):
    _miss(timed, 4)
    with pytest.raises(LockedOutError, match="Try again after"):
        timed.check("R. Mehta", "0000")


def test_the_right_pin_does_not_get_through_a_lockout(timed):
    _miss(timed, 4)
    with pytest.raises(LockedOutError):
        timed.check("R. Mehta", "0000")

    with pytest.raises(LockedOutError):
        timed.sign_in("R. Mehta", "4821")
    with pytest.raises(LockedOutError):
        timed.authorise("approve", "R. Mehta", "4821")
    assert timed.current is None


def test_a_right_pin_resets_the_count(timed):
    _miss(timed, 4)
    timed.check("R. Mehta", "4821")

    _miss(timed, 4)
    assert timed.check("R. Mehta", "4821").is_partner


def test_the_lockout_ends_after_fifteen_minutes(timed, clock):
    _miss(timed, 4)
    with pytest.raises(LockedOutError):
        timed.check("R. Mehta", "0000")

    clock.now += timedelta(minutes=14)
    with pytest.raises(LockedOutError):
        timed.check("R. Mehta", "4821")

    clock.now += timedelta(minutes=2)
    assert timed.sign_in("R. Mehta", "4821").is_partner


def test_a_lockout_is_recorded_on_the_audit_chain(timed, audit):
    _miss(timed, 4)
    with pytest.raises(LockedOutError):
        timed.check("R. Mehta", "0000")

    locked = [e for e in audit.entries() if e.event == "user_locked"]
    assert len(locked) == 1
    assert locked[0].actor == "R. Mehta"
    assert audit.verify().ok


def test_an_unknown_name_never_locks_and_reads_like_a_wrong_pin(timed):
    for _ in range(10):
        with pytest.raises(WrongPinError, match="do not match") as unknown:
            timed.check("Nobody", "0000")
    with pytest.raises(WrongPinError) as wrong:
        timed.check("R. Mehta", "0000")
    assert str(unknown.value) == str(wrong.value)


def test_an_older_database_gains_the_lockout_columns(tmp_path):
    """A firm's database from before lockout must open, not crash on login."""
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR UNIQUE, "
            "role VARCHAR, pin_hash VARCHAR, pin_salt VARCHAR, "
            "created_at DATETIME, disabled BOOLEAN)"
        )
    make_engine(path).dispose()
    old = make_engine(path)  # a second open must not try to add them again

    staff = People(old)
    staff.add("R. Mehta", PARTNER, "4821")
    assert staff.check("R. Mehta", "4821").is_partner
    old.dispose()


def test_parallel_guesses_each_count_as_a_strike(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    engine = make_engine(tmp_path / "race.db")
    staff = People(engine)
    staff.add("R. Mehta", PARTNER, "4821")

    def guess(_):
        try:
            staff.check("R. Mehta", "0000")
        except LockedOutError:
            return "locked"
        except WrongPinError:
            return "wrong"
        return "in"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(guess, range(12)))

    assert outcomes.count("wrong") == MAX_FAILED_PINS - 1
    assert outcomes.count("locked") == 12 - (MAX_FAILED_PINS - 1)
