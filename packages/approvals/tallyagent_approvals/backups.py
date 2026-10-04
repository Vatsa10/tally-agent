"""A same-day backup of the client's books before anything is posted in bulk.

One voucher posted wrongly is a voucher to delete. Forty posted wrongly, into a
client's real books, is an afternoon of undoing - unless there is a copy of the
books from this morning to go back to. So posting many tickets in one go is
refused until a backup of that company exists from today, and the refusal says
exactly how to take one.

Two ways a backup gets recorded, because Tally does not offer a reliable backup
over its XML interface:

- **A file copy.** Tally keeps each company as files in its data folder. When
  ``[tally] data_dir`` is configured, that folder is zipped into
  ``backups/<company>/<timestamp>/``, together with the firm database, so the
  register of who may approve is restored alongside the books it approved into.
- **An attestation.** Without a data folder to copy, a partner takes Tally's own
  backup (Alt+Y) and says so; the record carries their name and where they put
  it. The software cannot check that claim, which is why it is a partner's.

Either way it goes on the audit chain as ``backup_recorded``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import BackupRow
from tallyagent_core.errors import PolicyError

#: How many tickets in one go count as "many". Below this, an approve-all is a
#: handful of vouchers a person can undo by hand.
BULK_THRESHOLD = 5

FILE_COPY = "file_copy"
ATTESTED = "attested"


class BackupRequiredError(PolicyError):
    """A bulk posting was attempted with no backup of the books from today."""


class BackupNotPossibleError(PolicyError):
    """A file copy was asked for and there is nothing configured to copy."""


@dataclass(frozen=True, slots=True)
class Backup:
    company: str
    path: str
    taken_at: datetime
    taken_by: str
    method: str

    def describe(self) -> str:
        how = "file copy" if self.method == FILE_COPY else "attested Tally backup"
        return (
            f"{self.company}: {how} by {self.taken_by} at "
            f"{_local(self.taken_at):%Y-%m-%d %H:%M} - {self.path}"
        )


def _local(moment: datetime) -> datetime:
    """SQLite hands datetimes back without a zone; they were stored in UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone()


def _slug(company: str) -> str:
    """A folder name for a company. Tally names carry spaces, dots and slashes."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", company.strip()).strip("._")
    return cleaned or "company"


class BackupLog:
    """Which backups were taken, by whom, and whether one is recent enough.

    "Today" is the local calendar day: the partner who took a backup at nine
    this morning in Mumbai has a backup from today, whatever UTC thinks.
    """

    def __init__(
        self,
        engine: Engine,
        audit: object | None = None,
        data_dir: str = "",
        firm_db_path: str = "",
        root: str | Path = "backups",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine
        self.audit = audit
        self.data_dir = data_dir
        self.firm_db_path = firm_db_path
        self.root = Path(root)
        self.now = now or (lambda: datetime.now(UTC))

    @property
    def can_copy(self) -> bool:
        return bool(self.data_dir)

    # --- recording ------------------------------------------------------------

    def take(self, company: str, by: str) -> Backup:
        """Zip Tally's data folder and the firm database, and record it."""
        if not self.can_copy:
            raise BackupNotPossibleError(
                "no Tally data folder is configured, so there is nothing to copy. "
                "Set [tally] data_dir in config.toml, or take Tally's own backup "
                "(Alt+Y) and record it with: /backup confirm <where you saved it>"
            )
        source = Path(self.data_dir)
        if not source.is_dir():
            raise BackupNotPossibleError(
                f"the Tally data folder {source} does not exist. Check [tally] "
                "data_dir in config.toml (Tally shows it under F1 > Settings > Data)."
            )
        moment = self.now()
        folder = self.root / _slug(company) / f"{_local(moment):%Y%m%d-%H%M%S}"
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / "backup.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    zf.write(path, Path("tally") / path.relative_to(source))
            if self.firm_db_path and Path(self.firm_db_path).is_file():
                zf.writestr(
                    f"firm/{Path(self.firm_db_path).name}",
                    _sqlite_snapshot(Path(self.firm_db_path)),
                )
        return self._record(company, str(archive), by, FILE_COPY, moment)

    def attest(self, company: str, by: str, note: str) -> Backup:
        """Record a partner's word that they took Tally's own backup.

        The note is required: "I took a backup" with nowhere to find it is not
        a backup anybody can restore from.
        """
        if not note.strip():
            raise ValueError(
                "say where the backup is, e.g. /backup confirm D:\\TallyBackup\\"
            )
        return self._record(company, note.strip(), by, ATTESTED, self.now())

    def _record(
        self, company: str, path: str, by: str, method: str, moment: datetime
    ) -> Backup:
        row = BackupRow(
            company=company, path=path, taken_at=moment, taken_by=by, method=method
        )
        with Session(self.engine) as session:
            session.add(row)
            session.commit()
        if self.audit is not None:
            self.audit.append(  # type: ignore[attr-defined]
                "backup_recorded",
                actor=by,
                company=company,
                path=path,
                method=method,
            )
        return Backup(company, path, moment, by, method)

    # --- reading --------------------------------------------------------------

    def latest(self, company: str) -> Backup | None:
        with Session(self.engine) as session:
            row = session.exec(
                select(BackupRow)
                .where(BackupRow.company == company)
                .order_by(BackupRow.taken_at.desc())  # type: ignore[attr-defined]
            ).first()
        if row is None:
            return None
        return Backup(row.company, row.path, row.taken_at, row.taken_by, row.method)

    def today(self) -> date:
        return _local(self.now()).date()

    def taken_today(self, company: str) -> Backup | None:
        backup = self.latest(company)
        if backup is None or _local(backup.taken_at).date() != self.today():
            return None
        return backup

    # --- the gate -------------------------------------------------------------

    def how_to_take(self, company: str) -> str:
        if self.can_copy:
            return (
                "A partner takes one with /backup in the chat window, or "
                f'tallyagent backup take --client <client> --by "<partner name>" '
                f"(copies {self.data_dir})."
            )
        return (
            f"Take Tally's own backup of {company} (Alt+Y in TallyPrime), then a "
            "partner records it with /backup confirm <where you saved it>, or "
            'tallyagent backup take --client <client> --by "<partner name>" '
            '--confirm "<where you saved it>".'
        )

    def require_for(self, company: str, count: int) -> None:
        """Refuse a posting of ``count`` tickets with no backup from today."""
        if count < BULK_THRESHOLD or self.taken_today(company) is not None:
            return
        last = self.latest(company)
        seen = (
            f" The last one was {_local(last.taken_at):%Y-%m-%d}."
            if last is not None
            else " There is no backup of it on record."
        )
        raise BackupRequiredError(
            f"refusing to post {count} tickets to {company} in one go without a "
            f"backup of its books from today.{seen} {self.how_to_take(company)} "
            "Tickets can still be approved one at a time."
        )


def _sqlite_snapshot(path: Path) -> bytes:
    """A consistent copy of a live SQLite file.

    Copying the bytes of a database another connection is writing to can catch
    it mid-transaction; the backup API takes a snapshot that opens cleanly.
    """
    handle, temp = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    try:
        source = sqlite3.connect(path)
        target = sqlite3.connect(temp)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return Path(temp).read_bytes()
    finally:
        Path(temp).unlink(missing_ok=True)
