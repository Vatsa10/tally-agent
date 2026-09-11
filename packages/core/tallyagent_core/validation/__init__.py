"""Deterministic validation. Nothing here does I/O or calls a model."""

from tallyagent_core.validation.engine import validate
from tallyagent_core.validation.report import (
    RuleResult,
    Severity,
    ValidationReport,
)
from tallyagent_core.validation.rules import (
    ALL_RULES,
    DUPLICATE_WINDOW_DAYS,
    EDU_ALLOWED_DAYS,
    PostedVoucher,
    ValidationContext,
)

__all__ = [
    "ALL_RULES",
    "DUPLICATE_WINDOW_DAYS",
    "EDU_ALLOWED_DAYS",
    "PostedVoucher",
    "RuleResult",
    "Severity",
    "ValidationContext",
    "ValidationReport",
    "validate",
]
