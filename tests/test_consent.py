"""Which companies a partner has enabled, and what happens when the books move.

The rule being protected: a firm's real books are writable only because a named
person said so, about *those* books.
"""

from __future__ import annotations

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.consent import ConsentStore
from tallyagent_approvals.db import make_engine
from tallyagent_core.livemode import WriteScopeError


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def audit(engine):
    return AuditLog(engine)


@pytest.fixture
def store(engine, audit):
    return ConsentStore(engine, audit)


def test_nothing_is_writable_until_somebody_enables_it(store):
    assert not store.allows("Sharma Textiles")
    with pytest.raises(WriteScopeError, match="nobody has enabled it"):
        store.require("Sharma Textiles")


def test_a_grant_makes_one_company_writable(store):
    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")

    assert store.allows("Sharma Textiles", "guid-aaa")
    assert not store.allows("Gupta Traders", "guid-bbb"), "and only that one"


def test_a_grant_without_the_books_guid_is_refused(store):
    """A grant to a name is the loose behaviour this whole module replaces."""
    with pytest.raises(ValueError, match="without Tally's company GUID"):
        store.grant("Sharma Textiles", guid="", by="R. Mehta")


def test_different_books_under_an_enabled_name_are_refused(store):
    """Restoring another client's backup over the same company name, or pointing
    the install at a different machine, must not inherit the consent."""
    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")

    assert not store.allows("Sharma Textiles", "guid-zzz")
    with pytest.raises(WriteScopeError, match="not the one that was enabled"):
        store.require("Sharma Textiles", "guid-zzz")


def test_the_refusal_names_who_enabled_what(store):
    store.grant("Sharma Textiles", guid="guid-aaaaaaaaaaaaaaa", by="R. Mehta")

    reason = store.refusal("Sharma Textiles", "guid-zzzzzzzzzzzzzzz")

    assert "R. Mehta" in reason
    assert "guid-aaaaaaa" in reason and "guid-zzzzzzz" in reason


def test_a_caller_with_no_live_connection_checks_only_that_a_grant_exists(store):
    """The CLI listing consents has no Tally open; it must not read as refused."""
    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")

    assert store.allows("Sharma Textiles")


def test_revoking_stops_the_writes_and_keeps_the_history(store, engine):
    from sqlmodel import Session, select

    from tallyagent_approvals.db import ConsentRow

    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")
    assert store.revoke("Sharma Textiles", by="R. Mehta") == 1

    assert not store.allows("Sharma Textiles", "guid-aaa")
    with Session(engine) as session:
        rows = session.exec(select(ConsentRow)).all()
    assert len(rows) == 1, "the grant is closed, never deleted"
    assert rows[0].revoked_by == "R. Mehta"


def test_revoking_what_was_never_granted_is_not_an_error(store):
    assert store.revoke("Never Enabled Ltd", by="R. Mehta") == 0


def test_re_enabling_after_the_books_changed_supersedes_the_old_grant(store):
    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")
    store.grant("Sharma Textiles", guid="guid-bbb", by="S. Iyer", note="new backup")

    assert store.allows("Sharma Textiles", "guid-bbb")
    assert not store.allows("Sharma Textiles", "guid-aaa"), "the old books are out"
    assert store.active("Sharma Textiles").granted_by == "S. Iyer"


def test_every_grant_is_on_the_audit_chain(store, audit):
    """"Who allowed the software to write to these books" is the first question
    asked after anything goes wrong."""
    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")
    store.revoke("Sharma Textiles", by="S. Iyer")

    events = [(e.event, e.actor) for e in audit.entries()]

    assert ("consent_granted", "R. Mehta") in events
    assert ("consent_revoked", "S. Iyer") in events
    assert audit.verify().ok


def test_the_listing_shows_only_what_is_live_now(store):
    store.grant("Sharma Textiles", guid="guid-aaa", by="R. Mehta")
    store.grant("Gupta Traders", guid="guid-bbb", by="R. Mehta")
    store.revoke("Gupta Traders", by="R. Mehta")

    assert [c.company for c in store.all_active()] == ["Sharma Textiles"]


def test_a_grant_reads_as_a_sentence(store):
    granted = store.grant(
        "Sharma Textiles", guid="guid-aaa", by="R. Mehta", machine="FRONT-DESK"
    )

    assert "Sharma Textiles" in granted.describe()
    assert "R. Mehta" in granted.describe()
    assert "FRONT-DESK" in granted.describe()


def test_the_table_remembers_a_company_it_has_ruled_on(store):
    store.grant("TA-Demo Traders", guid="guid-aaa", by="R. Mehta")
    store.revoke("TA-Demo Traders", by="R. Mehta")

    assert store.known("TA-Demo Traders"), "so the name prefix cannot override it"
    assert not store.known("Never Heard Of Ltd")
