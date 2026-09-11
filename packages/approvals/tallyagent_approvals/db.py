"""Local SQLite state: approvals, audit chain, idempotency, memory.

Everything here stays on the machine. Nothing in this database is ever sent to a
model; the context builder reads from it selectively and logs what it took.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

from tallyagent_core.idempotency import IdempotencyRecord


def _now() -> datetime:
    return datetime.now(UTC)


class ApprovalRow(SQLModel, table=True):
    """One queued mutation."""

    __tablename__ = "approvals"

    id: int | None = Field(default=None, primary_key=True)
    ticket: str = Field(index=True, unique=True)
    company: str = Field(index=True)
    action_type: str = Field(index=True)
    status: str = Field(default="pending", index=True)  # pending|approved|rejected|failed
    summary: str = ""
    amount: str = "0"
    source: str = "chat"
    idempotency_key: str = ""
    raw_xml: str = ""
    # JSON blobs: the voucher, the validation report, and the tool payload.
    voucher_json: str = "null"
    validation_json: str = "null"
    payload_json: str = "{}"
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None
    decided_by: str = ""
    decision_reason: str = ""
    result_json: str = "null"


class AuditRow(SQLModel, table=True):
    """One link in the append-only hash chain."""

    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    sequence: int = Field(index=True, unique=True)
    at: datetime = Field(default_factory=_now)
    event: str = Field(index=True)
    actor: str = ""
    payload_json: str = "{}"
    prev_hash: str = ""
    hash: str = ""


class IdempotencyRow(SQLModel, table=True):
    """Persisted idempotency keys - the guarantee must survive a restart."""

    __tablename__ = "idempotency"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    company: str = ""
    status: str = "pending"
    result_json: str = "{}"
    created_at: datetime = Field(default_factory=_now)


class MemoryRow(SQLModel, table=True):
    """Per-company learned facts: ledger aliases, party aliases, corrections."""

    __tablename__ = "memory"

    id: int | None = Field(default=None, primary_key=True)
    company: str = Field(index=True)
    kind: str = Field(index=True)  # ledger_alias | party_alias | correction | pattern
    key: str = Field(index=True)
    value: str = ""
    hits: int = 0
    updated_at: datetime = Field(default_factory=_now)


class EgressRow(SQLModel, table=True):
    """What left the machine, per model request. See docs/SECURITY.md."""

    __tablename__ = "egress"

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_now)
    destination: str = ""
    provider: str = ""
    model: str = ""
    bytes_sent: int = 0
    fields: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: str = "0"


def make_engine(db_path: str | Path = "tallyagent.db", echo: bool = False) -> Engine:
    """Create the engine and the schema.

    ``:memory:`` needs a shared static pool or each connection gets its own
    empty database, which makes tests silently pass against nothing.
    """
    if str(db_path) in (":memory:", "sqlite://"):
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}", echo=echo)
    SQLModel.metadata.create_all(engine)
    return engine


class SqlIdempotencyStore:
    """``IdempotencyStore`` backed by SQLite."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, key: str) -> IdempotencyRecord | None:
        import json

        with Session(self.engine) as session:
            row = session.exec(
                select(IdempotencyRow).where(IdempotencyRow.key == key)
            ).first()
            if row is None:
                return None
            return IdempotencyRecord(
                key=row.key,
                company=row.company,
                status=row.status,
                result=json.loads(row.result_json),
                created_at=row.created_at,
            )

    def put(self, record: IdempotencyRecord) -> None:
        import json

        with Session(self.engine) as session:
            row = session.exec(
                select(IdempotencyRow).where(IdempotencyRow.key == record.key)
            ).first()
            if row is None:
                row = IdempotencyRow(key=record.key, created_at=record.created_at)
            row.company = record.company
            row.status = record.status
            row.result_json = json.dumps(record.result)
            session.add(row)
            session.commit()
