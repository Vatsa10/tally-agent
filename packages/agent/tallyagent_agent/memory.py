"""Per-company memory: aliases, recurring patterns, learned corrections.

Small and explicit on purpose. Everything here is a fact a human taught the
system by correcting it, and every entry is inspectable and deletable. Nothing
is inferred from data the user did not act on.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import ApprovalRow, MemoryRow

LEDGER_ALIAS = "ledger_alias"
PARTY_ALIAS = "party_alias"
PATTERN = "pattern"
CORRECTION = "correction"


@dataclass(slots=True)
class MemoryEntry:
    kind: str
    key: str
    value: str
    hits: int


class Memory:
    def __init__(self, engine: Engine, company: str) -> None:
        self.engine = engine
        self.company = company

    # --- read ---------------------------------------------------------------

    def all(self, kind: str = "") -> list[MemoryEntry]:
        with Session(self.engine) as session:
            statement = select(MemoryRow).where(MemoryRow.company == self.company)
            if kind:
                statement = statement.where(MemoryRow.kind == kind)
            rows = list(session.exec(statement))
        return [MemoryEntry(r.kind, r.key, r.value, r.hits) for r in rows]

    def ledger_aliases(self) -> dict[str, str]:
        return {e.key: e.value for e in self.all(LEDGER_ALIAS)}

    def party_aliases(self) -> dict[str, str]:
        return {e.key: e.value for e in self.all(PARTY_ALIAS)}

    def get(self, kind: str, key: str) -> str | None:
        with Session(self.engine) as session:
            row = session.exec(
                select(MemoryRow)
                .where(MemoryRow.company == self.company)
                .where(MemoryRow.kind == kind)
                .where(MemoryRow.key == key)
            ).first()
        return row.value if row else None

    # --- write --------------------------------------------------------------

    def remember(self, kind: str, key: str, value: str) -> MemoryEntry:
        """Upsert. Repeating a lesson increments its hit count rather than
        duplicating it, so the most-reinforced correction is visible."""
        with Session(self.engine) as session:
            row = session.exec(
                select(MemoryRow)
                .where(MemoryRow.company == self.company)
                .where(MemoryRow.kind == kind)
                .where(MemoryRow.key == key)
            ).first()
            if row is None:
                row = MemoryRow(company=self.company, kind=kind, key=key)
            row.value = value
            row.hits += 1
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            return MemoryEntry(kind, key, value, row.hits)

    def forget(self, kind: str, key: str) -> bool:
        with Session(self.engine) as session:
            row = session.exec(
                select(MemoryRow)
                .where(MemoryRow.company == self.company)
                .where(MemoryRow.kind == kind)
                .where(MemoryRow.key == key)
            ).first()
            if row is None:
                return False
            session.delete(row)
            session.commit()
        return True

    # --- derived ------------------------------------------------------------

    def recurring_patterns(self, minimum: int = 3) -> list[MemoryEntry]:
        """Party + action combinations seen often enough to be worth noticing.

        Read from the approvals history rather than stored separately: the
        queue already knows what this company actually does month after month.
        """
        with Session(self.engine) as session:
            rows = list(
                session.exec(
                    select(ApprovalRow)
                    .where(ApprovalRow.company == self.company)
                    .where(ApprovalRow.status == "approved")
                )
            )
        counts = Counter(f"{row.action_type}" for row in rows)
        return [
            MemoryEntry(PATTERN, action, f"approved {count} time(s)", count)
            for action, count in counts.most_common()
            if count >= minimum
        ]

    def summary(self, limit: int = 12) -> str:
        """A compact block for the system prompt. Capped: memory must not be
        able to crowd out the actual question."""
        aliases = list(self.ledger_aliases().items())[:limit]
        if not aliases:
            return ""
        lines = [f'  "{key}" means the ledger "{value}"' for key, value in aliases]
        return "Learned name mappings for this company:\n" + "\n".join(lines)
