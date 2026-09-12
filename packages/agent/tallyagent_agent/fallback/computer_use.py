"""Tier 3: bounded, recorded, approval-gated computer use.

This is the escape hatch of last resort, and it is built to be unattractive:
disabled by default, hard step limit, every frame and action recorded, and any
step that could change data stops the loop for human approval. If this code is
running regularly, the answer is not to loosen it - it is to build the Tier 1
tool whose absence the ``fallback_used`` event is already naming.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tallyagent_agent.perception.screen import WindowBounds, capture, find_tally_window
from tallyagent_agent.tiers import TierRouter
from tallyagent_core.errors import NotConfiguredError, PolicyError
from tallyagent_llm.provider import Image, Message
from tallyagent_llm.router import Router

log = logging.getLogger(__name__)

#: Keystrokes and clicks that can create, alter or delete data in Tally.
#: Anything matching stops for approval. The list is deliberately broad: a false
#: positive costs one approval click, a false negative costs a wrong voucher.
MUTATING_KEYS = frozenset(
    {"enter", "ctrl+a", "ctrl+enter", "alt+d", "alt+x", "f7", "f8", "f9", "y"}
)

SYSTEM = """\
You are operating TallyPrime through its keyboard interface because no API tool \
exists for this task. Reply with JSON only:
{"action": "key"|"click"|"done", "key": string, "x": int, "y": int, "why": string}
Prefer keystrokes to clicks. Say "done" as soon as the task is complete. Never \
accept or save a voucher without being told to."""


@dataclass(slots=True)
class FallbackStep:
    index: int
    action: str
    detail: str
    why: str = ""
    screenshot: bytes = b""
    needs_approval: bool = False
    approved: bool = False
    executed: bool = False
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        """Serialisable record - the screenshot stays local, only its size goes
        into the log."""
        return {
            "index": self.index,
            "action": self.action,
            "detail": self.detail,
            "why": self.why,
            "screenshot_bytes": len(self.screenshot),
            "needs_approval": self.needs_approval,
            "approved": self.approved,
            "executed": self.executed,
            "at": self.at,
        }


@dataclass(slots=True)
class FallbackSession:
    task: str
    steps: list[FallbackStep] = field(default_factory=list)
    completed: bool = False
    stopped_reason: str = ""

    @property
    def recording(self) -> list[dict[str, Any]]:
        return [step.as_dict() for step in self.steps]


def is_mutating(action: str, key: str) -> bool:
    """A click could be anything, so clicks are always treated as mutating."""
    if action == "click":
        return True
    return key.strip().lower() in MUTATING_KEYS


def press(key: str) -> None:
    """Send a keystroke. Requires the fallback extra."""
    try:
        import pyautogui  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotConfiguredError(
            "the Tier 3 fallback needs: uv sync --extra fallback"
        ) from exc
    if "+" in key:
        pyautogui.hotkey(*[part.strip() for part in key.split("+")])
    else:
        pyautogui.press(key)


def click(x: int, y: int) -> None:
    try:
        import pyautogui  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotConfiguredError(
            "the Tier 3 fallback needs: uv sync --extra fallback"
        ) from exc
    pyautogui.click(x, y)


#: Called with a FallbackStep that could mutate; returns True to proceed.
ApprovalHook = Callable[[FallbackStep], Awaitable[bool]]


class ComputerUseFallback:
    def __init__(
        self,
        router: Router,
        tiers: TierRouter,
        on_fallback_used: Callable[[str], None] | None = None,
        approve: ApprovalHook | None = None,
        locate: Callable[[], WindowBounds | None] = find_tally_window,
        grab: Callable[[WindowBounds], bytes] = capture,
        do_key: Callable[[str], None] = press,
        do_click: Callable[[int, int], None] = click,
        spotlight: Any = None,
        ensure_visible: Callable[[], tuple[WindowBounds | None, str]] | None = None,
    ) -> None:
        self.router = router
        self.tiers = tiers
        self.on_fallback_used = on_fallback_used
        # No approval hook means no mutating step is ever executed. Defaulting
        # to "allow" here would quietly undo the entire gate.
        self.approve = approve
        self._locate = locate
        self._grab = grab
        # The spotlight narrates and slows down what is about to happen. It
        # wraps the key and click functions rather than replacing them, so the
        # approval gate above is untouched by whether anyone is watching.
        self.spotlight = spotlight
        if spotlight is not None:
            spotlight._key = do_key
            spotlight._click = do_click
            self._key = spotlight.key
            self._click = spotlight.click
        else:
            self._key = do_key
            self._click = do_click
        self._ensure_visible = ensure_visible

    async def run(self, task: str) -> FallbackSession:
        self.tiers.require_fallback()
        session = FallbackSession(task=task)
        if self.on_fallback_used is not None:
            self.on_fallback_used(task)
        log.warning("fallback_used: %s - build a Tier 1 tool for this", task)

        if self._ensure_visible is not None:
            # Keystrokes land wherever focus is. Tally has to be in front
            # before a single one is sent, and if it cannot be, nothing is.
            bounds, reason = self._ensure_visible()
            if bounds is None:
                session.stopped_reason = reason
                return session
        else:
            bounds = self._locate()
            if bounds is None:
                session.stopped_reason = "no TallyPrime window is open"
                return session

        history: list[Message] = [Message(role="system", content=SYSTEM)]
        limit = self.tiers.config.fallback_max_steps

        for index in range(1, limit + 1):
            png = self._grab(bounds)
            history.append(
                Message(
                    role="user",
                    content=f"Task: {task}. What is the next action?",
                    images=[Image(data=png, media_type="image/png")],
                )
            )
            completion = await self.router.complete(history, max_tokens=300)
            history.append(Message(role="assistant", content=completion.text))

            action, key, x, y, why = _parse_action(completion.text)
            if action == "done":
                session.completed = True
                session.stopped_reason = why or "the model reported the task complete"
                return session
            if action not in ("key", "click"):
                session.stopped_reason = f"unparseable action: {completion.text[:120]}"
                return session

            step = FallbackStep(
                index=index,
                action=action,
                detail=key if action == "key" else f"{x},{y}",
                why=why,
                screenshot=png,
                needs_approval=is_mutating(action, key),
            )
            session.steps.append(step)

            if step.needs_approval:
                if self.approve is None:
                    session.stopped_reason = (
                        f"step {index} ({step.detail}) could change data and no "
                        "approver is configured"
                    )
                    return session
                step.approved = await self.approve(step)
                if not step.approved:
                    session.stopped_reason = f"step {index} was rejected by the approver"
                    return session

            if self.spotlight is not None:
                self.spotlight.announce(why)
            if action == "key":
                self._key(key)
            else:
                self._click(x, y)
            step.executed = True

        session.stopped_reason = f"hit the {limit}-step limit without finishing"
        return session


def _parse_action(raw: str) -> tuple[str, str, int, int, str]:
    import json
    import re

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "", "", 0, 0, ""
    return (
        str(data.get("action") or ""),
        str(data.get("key") or ""),
        int(data.get("x") or 0),
        int(data.get("y") or 0),
        str(data.get("why") or ""),
    )


def require_enabled(tiers: TierRouter) -> None:
    """Convenience for callers that want the gate without a session."""
    if not tiers.config.fallback_enabled:
        raise PolicyError("Tier 3 computer-use fallback is disabled.")
