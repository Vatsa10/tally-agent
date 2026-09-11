"""Approval policy: action type -> how it gets approved.

Default for an action type nobody has configured is ``manual``. Promotion to
auto-approve is a deliberate human decision, informed by
``tallyagent approvals stats``; nothing in this system promotes itself.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path


class ApprovalMode(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"
    AUTO_BELOW_AMOUNT = "auto_below_amount"


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    mode: ApprovalMode = ApprovalMode.MANUAL
    max_amount: Decimal | None = None

    def auto_approves(self, amount: Decimal) -> bool:
        if self.mode is ApprovalMode.AUTO:
            return True
        if self.mode is ApprovalMode.AUTO_BELOW_AMOUNT:
            # Missing max_amount on an auto_below_amount rule is a config bug;
            # fail closed rather than auto-approving everything.
            return self.max_amount is not None and amount < self.max_amount
        return False


@dataclass(slots=True)
class Policy:
    actions: dict[str, ActionPolicy] = field(default_factory=dict)
    allow_validation_override: bool = False

    def for_action(self, action_type: str) -> ActionPolicy:
        return self.actions.get(action_type, ActionPolicy())

    def requires_approval(self, action_type: str, amount: Decimal) -> bool:
        return not self.for_action(action_type).auto_approves(amount)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Policy:
        actions: dict[str, ActionPolicy] = {}
        raw_actions = data.get("actions") or {}
        if not isinstance(raw_actions, dict):
            raise ValueError("policy [actions] must be a table")
        for name, spec in raw_actions.items():
            if not isinstance(spec, dict):
                raise ValueError(f"policy for {name!r} must be a table")
            mode = ApprovalMode(str(spec.get("mode", "manual")))
            raw_max = spec.get("max_amount")
            actions[name] = ActionPolicy(
                mode=mode,
                max_amount=Decimal(str(raw_max)) if raw_max is not None else None,
            )
        overrides = data.get("overrides") or {}
        allow = bool(
            overrides.get("allow_validation_override", False)
            if isinstance(overrides, dict)
            else False
        )
        return cls(actions=actions, allow_validation_override=allow)

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        with Path(path).open("rb") as fh:
            return cls.from_dict(tomllib.load(fh))

    @classmethod
    def default(cls) -> Policy:
        """Everything manual. What you get with no policy file at all."""
        return cls()
