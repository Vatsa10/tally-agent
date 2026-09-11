from __future__ import annotations

from collections.abc import Callable, Sequence

from tallyagent_core.models import Voucher
from tallyagent_core.validation.report import ValidationReport
from tallyagent_core.validation.rules import ALL_RULES, ValidationContext

Rule = Callable[[Voucher, ValidationContext], object]


def validate(
    voucher: Voucher,
    ctx: ValidationContext,
    rules: Sequence[Rule] = ALL_RULES,
) -> ValidationReport:
    """Run every rule. Rules never raise; a rule that blows up is itself a
    failure, because a validator that crashes must not become a validator that
    is skipped."""
    report = ValidationReport()
    for rule in rules:
        try:
            report.add(rule(voucher, ctx))  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - deliberate: never skip a rule
            from tallyagent_core.validation.report import RuleResult

            report.add(
                RuleResult(
                    getattr(rule, "__name__", "unknown_rule"),
                    False,
                    f"rule raised {type(exc).__name__}: {exc}",
                )
            )
    return report
