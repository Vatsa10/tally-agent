"""Which real companies this install may write to, and who said so.

The guard this replaces was a name prefix: live mode would only write to a
company called ``TA-something``. That is exactly right for a demo install and
useless for a practice, because no real books are called that - so the choice
was rename a client's company or turn the guard off, and both are wrong answers.

Consent is a record instead of a rule: a partner enables one company, and the
grant carries their name, the time, and the machine. Two properties make it
worth having rather than a line in a config file:

- **It is bound to the books, not the name.** Tally's company GUID is stored
  with the grant. Rename the company, restore a different company under the
  same name, or point the install at another machine's Tally, and the GUID no
  longer matches - so writes refuse until a partner looks and re-consents.
- **It is on the audit chain.** "Who allowed the software to write to these
  books" is the first question after anything goes wrong, and a config file
  cannot answer it.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import ConsentRow
from tallyagent_core.livemode import WriteScopeError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Consent:
    company: str
    guid: str
    granted_by: str
    granted_at: datetime
    machine: str = ""
    note: str = ""

    def describe(self) -> str:
        when = self.granted_at.strftime("%d-%b-%Y")
        where = f" on {self.machine}" if self.machine else ""
        return f"{self.company}: enabled by {self.granted_by} on {when}{where}"


class ConsentStore:
    """The consent table. Reads are cheap and happen before every write."""

    def __init__(self, engine: Engine, audit: object | None = None) -> None:
        self.engine = engine
        self.audit = audit

    def grant(
        self,
        company: str,
        guid: str,
        by: str,
        note: str = "",
        machine: str = "",
    ) -> Consent:
        """Enable a company. ``by`` is a real person's name, checked by the caller.

        A company with no GUID is refused. Tally reports one for every company
        it has open, and a grant without it is a grant to a name - which is the
        loose behaviour this exists to end.
        """
        company = (company or "").strip()
        guid = (guid or "").strip()
        if not company:
            raise ValueError("a consent needs a company name")
        if not guid:
            raise ValueError(
                f"refusing to enable {company!r} without Tally's company GUID: "
                "consent follows the books, not the name. Load the company in "
                "Tally so its GUID can be read, then try again."
            )

        machine = machine or socket.gethostname()
        with Session(self.engine) as session:
            for row in self._rows(session, company):
                if row.revoked_at is None:
                    row.revoked_at = datetime.now(UTC)
                    row.revoked_by = by
                    session.add(row)
            row = ConsentRow(
                company=company, guid=guid, granted_by=by, machine=machine, note=note
            )
            session.add(row)
            session.commit()
            granted = Consent(
                company=row.company,
                guid=row.guid,
                granted_by=row.granted_by,
                granted_at=row.granted_at,
                machine=row.machine,
                note=row.note,
            )

        self._note("consent_granted", by, company=company, guid=guid, note=note)
        return granted

    def revoke(self, company: str, by: str) -> int:
        with Session(self.engine) as session:
            live = [r for r in self._rows(session, company) if r.revoked_at is None]
            for row in live:
                row.revoked_at = datetime.now(UTC)
                row.revoked_by = by
                session.add(row)
            session.commit()
        if live:
            self._note("consent_revoked", by, company=company)
        return len(live)

    def active(self, company: str) -> Consent | None:
        with Session(self.engine) as session:
            rows = [r for r in self._rows(session, company) if r.revoked_at is None]
        if not rows:
            return None
        row = rows[-1]
        return Consent(
            company=row.company,
            guid=row.guid,
            granted_by=row.granted_by,
            granted_at=row.granted_at,
            machine=row.machine,
            note=row.note,
        )

    def all_active(self) -> list[Consent]:
        with Session(self.engine) as session:
            rows = session.exec(
                select(ConsentRow).where(ConsentRow.revoked_at == None)  # noqa: E711
            ).all()
        return [
            Consent(
                company=r.company,
                guid=r.guid,
                granted_by=r.granted_by,
                granted_at=r.granted_at,
                machine=r.machine,
                note=r.note,
            )
            for r in sorted(rows, key=lambda r: r.company)
        ]

    # --- the guard -----------------------------------------------------------

    def known(self, company: str) -> bool:
        """Has this company ever been enabled or revoked here?

        Paired with :meth:`refusal` in the live-mode guard: a company the table
        has heard of is decided by the table, so revoking a grant actually stops
        the writes even for a company whose name matches the install's own
        prefix.
        """
        with Session(self.engine) as session:
            return bool(self._rows(session, company))

    def allows(self, company: str, guid: str = "") -> bool:
        """May we write to this company, as it is loaded right now?

        ``guid`` is what Tally currently reports for that company. Passing it is
        how the binding is enforced; passing nothing checks only that a live
        grant exists, which is all a caller without a live connection can do.
        """
        return not self.refusal(company, guid)

    def refusal(self, company: str, guid: str = "") -> str:
        """Why this company may not be written to, or "" if it may.

        A reason rather than a bool, because the two refusals need different
        sentences and the person reading them is doing different things: one has
        to enable the company, the other has to work out why Tally is showing
        different books under a name that was already enabled.
        """
        consent = self.active(company)
        if consent is None:
            return (
                f"refusing to write to {company!r}: nobody has enabled it. A "
                "partner enables a company once, with "
                f'`tallyagent consent add "{company}"`, and the grant is '
                "recorded with their name."
            )
        if guid and consent.guid and guid.strip() != consent.guid:
            return (
                f"refusing to write to {company!r}: the company Tally has open "
                f"is not the one that was enabled. {consent.granted_by} enabled "
                f"the books with GUID {consent.guid[:12]}..., and Tally is "
                f"showing {guid.strip()[:12]}.... Books that were renamed, "
                "restored from another backup, or on another machine have to be "
                "enabled again by a partner."
            )
        return ""

    def require(self, company: str, guid: str = "") -> Consent:
        reason = self.refusal(company, guid)
        if reason:
            raise WriteScopeError(reason)
        consent = self.active(company)
        assert consent is not None  # noqa: S101 - refusal() proved it
        return consent

    # --- helpers -------------------------------------------------------------

    def _rows(self, session: Session, company: str) -> list[ConsentRow]:
        return list(
            session.exec(
                select(ConsentRow)
                .where(ConsentRow.company == (company or "").strip())
                .order_by(ConsentRow.id)  # type: ignore[arg-type]
            ).all()
        )

    def _note(self, event: str, actor: str, **payload: object) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event, actor=actor, **payload)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - never block a grant on the log
            log.exception("could not record %s", event)
