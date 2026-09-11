from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"      # blocks the write
    WARNING = "warning"  # shown to the approver, does not block
    INFO = "info"


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: str
    passed: bool
    message: str = ""
    severity: Severity = Severity.ERROR
    # Machine-readable extras: fuzzy suggestions, the duplicate's voucher no, ...
    details: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationReport:
    """Result of running every deterministic rule against one draft.

    ``ok`` is the gate: no error-severity failure. Warnings surface in the
    approval diff but never block.
    """

    results: list[RuleResult] = field(default_factory=list)
    overridden_by: str | None = None
    override_reason: str | None = None

    def add(self, result: RuleResult) -> None:
        self.results.append(result)

    @property
    def failures(self) -> list[RuleResult]:
        return [r for r in self.results if not r.passed and r.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[RuleResult]:
        return [
            r for r in self.results if not r.passed and r.severity is Severity.WARNING
        ]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def blocks_enqueue(self) -> bool:
        """Failures block enqueue unless a human overrode them, with a reason."""
        if self.ok:
            return False
        return self.overridden_by is None

    def override(self, user: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("an override must carry a reason; it goes in the audit log")
        self.overridden_by = user
        self.override_reason = reason

    def summary(self) -> str:
        if self.ok:
            n = len(self.results)
            warn = f", {len(self.warnings)} warning(s)" if self.warnings else ""
            return f"All {n} checks passed{warn}."
        lines = [f"{len(self.failures)} check(s) failed:"]
        lines += [f"  - {r.rule}: {r.message}" for r in self.failures]
        lines += [f"  ! {r.rule}: {r.message}" for r in self.warnings]
        return "\n".join(lines)
