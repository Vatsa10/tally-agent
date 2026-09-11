from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

# GST state codes 01-38 plus 97 (other territory) and 99 (centre). Used to decide
# CGST+SGST (intra-state) vs IGST (inter-state).
VALID_STATE_CODES = {f"{n:02d}" for n in range(1, 39)} | {"97", "99"}


class Period(BaseModel):
    """A financial period. ``locked_before`` closes months against back-posting."""

    start: date
    end: date
    locked_before: date | None = None

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: date, info: object) -> date:
        start = getattr(info, "data", {}).get("start")
        if start and v < start:
            raise ValueError("period end must not precede start")
        return v

    def contains(self, when: date) -> bool:
        return self.start <= when <= self.end

    def is_locked(self, when: date) -> bool:
        return self.locked_before is not None and when < self.locked_before


class Company(BaseModel):
    """The Tally company we are acting on behalf of."""

    name: str = Field(min_length=1)
    state_code: str = "27"
    gstin: str | None = None
    period: Period | None = None

    @field_validator("state_code")
    @classmethod
    def _known_state(cls, v: str) -> str:
        v = v.strip().zfill(2)
        if v not in VALID_STATE_CODES:
            raise ValueError(f"unknown GST state code {v!r}")
        return v
