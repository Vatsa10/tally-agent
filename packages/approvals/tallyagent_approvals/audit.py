"""Append-only audit log with a SHA-256 hash chain.

Each record hashes its own content together with the previous record's hash, so
editing or deleting any past entry breaks every hash after it. This is
tamper-*evidence*, not tamper-proofing: someone with write access to the file
can rebuild the whole chain. What it defends against is the realistic case - a
row quietly changed or dropped, which ``tallyagent audit verify`` then catches.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import AuditRow

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    """Stable JSON. Key order and separators must never vary, or a re-serialised
    identical payload would hash differently and look like tampering."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(
    sequence: int, at: datetime, event: str, actor: str, payload: dict[str, Any], prev: str
) -> str:
    material = "|".join(
        [str(sequence), at.isoformat(), event, actor, _canonical(payload), prev]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AuditEntry:
    sequence: int
    at: datetime
    event: str
    actor: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(slots=True)
class VerifyResult:
    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str = ""

    def __str__(self) -> str:
        if self.ok:
            return f"Audit chain intact: {self.checked} record(s) verified."
        return (
            f"Audit chain BROKEN at sequence {self.broken_at}: {self.reason} "
            f"({self.checked} record(s) checked)."
        )


class AuditLog:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def append(
        self, event: str, actor: str = "system", **payload: Any
    ) -> AuditEntry:
        """Add one record. Never updates or deletes - that is the whole point."""
        with Session(self.engine) as session:
            last = session.exec(
                select(AuditRow).order_by(AuditRow.sequence.desc())  # type: ignore[attr-defined]
            ).first()
            sequence = (last.sequence + 1) if last else 1
            prev_hash = last.hash if last else GENESIS
            at = datetime.now(UTC)
            digest = compute_hash(sequence, at, event, actor, payload, prev_hash)
            row = AuditRow(
                sequence=sequence,
                at=at,
                event=event,
                actor=actor,
                payload_json=_canonical(payload),
                prev_hash=prev_hash,
                hash=digest,
            )
            session.add(row)
            session.commit()
            return AuditEntry(sequence, at, event, actor, payload, prev_hash, digest)

    def entries(self, limit: int | None = None) -> list[AuditEntry]:
        with Session(self.engine) as session:
            statement = select(AuditRow).order_by(AuditRow.sequence)  # type: ignore[arg-type]
            rows = list(session.exec(statement))
        if limit:
            rows = rows[-limit:]
        return [
            AuditEntry(
                sequence=row.sequence,
                at=row.at,
                event=row.event,
                actor=row.actor,
                payload=json.loads(row.payload_json),
                prev_hash=row.prev_hash,
                hash=row.hash,
            )
            for row in rows
        ]

    def verify(self) -> VerifyResult:
        """Walk the chain, recomputing every hash."""
        with Session(self.engine) as session:
            rows = list(
                session.exec(select(AuditRow).order_by(AuditRow.sequence))  # type: ignore[arg-type]
            )

        expected_prev = GENESIS
        expected_sequence = 1
        for index, row in enumerate(rows, start=1):
            if row.sequence != expected_sequence:
                return VerifyResult(
                    False,
                    index - 1,
                    row.sequence,
                    f"sequence jumped: expected {expected_sequence}, found {row.sequence}",
                )
            if row.prev_hash != expected_prev:
                return VerifyResult(
                    False, index - 1, row.sequence, "previous-hash link does not match"
                )
            at = row.at if row.at.tzinfo else row.at.replace(tzinfo=UTC)
            recomputed = compute_hash(
                row.sequence,
                at,
                row.event,
                row.actor,
                json.loads(row.payload_json),
                row.prev_hash,
            )
            if recomputed != row.hash:
                return VerifyResult(
                    False, index - 1, row.sequence, "record content does not match its hash"
                )
            expected_prev = row.hash
            expected_sequence += 1
        return VerifyResult(True, len(rows))
