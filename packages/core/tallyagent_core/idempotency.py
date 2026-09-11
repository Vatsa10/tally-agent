"""Client-generated idempotency keys.

Tally has no idempotency of its own: post the same sales voucher twice and you
get two vouchers and a wrong turnover. So every write carries a key we generate
and persist *before* the request goes out; a replay of a key we have already
recorded as succeeded is a no-op that returns the original result.

The key is derived from the voucher's content fingerprint plus the company, so
"the same invoice submitted twice" collides even when the second submission is
a fresh object built from a re-uploaded image.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from tallyagent_core.models import Voucher

KEY_PREFIX = "ta1"


def make_key(company: str, voucher: Voucher, salt: str = "") -> str:
    """Deterministic key for a voucher in a company.

    ``salt`` lets a user deliberately post a genuine duplicate (same invoice,
    legitimately entered twice) by passing e.g. the second voucher number.
    """
    digest = hashlib.sha256(
        f"{company}|{voucher.fingerprint()}|{salt}".encode()
    ).hexdigest()
    return f"{KEY_PREFIX}_{digest[:32]}"


@dataclass(slots=True)
class IdempotencyRecord:
    key: str
    company: str
    status: str = "pending"  # pending | succeeded | failed
    result: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class IdempotencyStore(Protocol):
    """Persistence boundary. The SQLite implementation lives in approvals.db."""

    def get(self, key: str) -> IdempotencyRecord | None: ...
    def put(self, record: IdempotencyRecord) -> None: ...


class InMemoryIdempotencyStore:
    """Used by tests and by the fake Tally harness."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}

    def get(self, key: str) -> IdempotencyRecord | None:
        return self._records.get(key)

    def put(self, record: IdempotencyRecord) -> None:
        self._records[record.key] = record


@dataclass(slots=True)
class ReplayDecision:
    """What the caller should do about a key it is about to use."""

    should_execute: bool
    reason: str
    existing: IdempotencyRecord | None = None


def check(store: IdempotencyStore, key: str, company: str) -> ReplayDecision:
    """Decide whether a write bearing ``key`` should actually run.

    A previously *failed* attempt is allowed to re-run — the failure may have
    been a Tally restart. A previously *succeeded* one never re-runs. A
    ``pending`` one is treated as in-flight and also blocked, so two concurrent
    submissions of the same invoice cannot both post.
    """
    existing = store.get(key)
    if existing is None:
        store.put(IdempotencyRecord(key=key, company=company))
        return ReplayDecision(True, "new key", None)
    if existing.status == "succeeded":
        return ReplayDecision(False, "replay of a completed write", existing)
    if existing.status == "pending":
        return ReplayDecision(False, "an identical write is already in flight", existing)
    return ReplayDecision(True, "retry after previous failure", existing)


def record_success(store: IdempotencyStore, key: str, result: dict[str, str]) -> None:
    rec = store.get(key)
    if rec is None:
        raise KeyError(f"no idempotency record for {key}")
    rec.status = "succeeded"
    rec.result = result
    store.put(rec)


def record_failure(store: IdempotencyStore, key: str, error: str) -> None:
    rec = store.get(key)
    if rec is None:
        raise KeyError(f"no idempotency record for {key}")
    rec.status = "failed"
    rec.result = {"error": error}
    store.put(rec)
