"""Getting a local TallyPrime back to answering, without a human.

Three things go wrong on a real desk, and all three were hit repeatedly while
building this:

1. Tally is not running, or has crashed - a malformed TDL does both.
2. Tally is running but sitting on the licence screen, because it cannot reach
   the licence gateway. Nothing answers on the XML port until someone chooses
   "Continue In Educational Mode".
3. Tally is past that screen but has no company loaded, so every read comes back
   empty even though the port is healthy.

Left to a person, each of these turns a bookkeeping session into a support
call. This module is what the client calls when a request fails, and what
``tallyagent doctor`` calls on purpose.

Nothing here touches data. It starts a process, brings a window forward and
presses the one key that clears the licence screen. Anything that could change
a voucher is Tier 3's business and goes through Tier 3's approval gate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from tallyagent_tally import install
from tallyagent_tally.client import TallyClient, TallyConfig

log = logging.getLogger(__name__)

#: Tally needs a moment after a keypress before the next check means anything.
SETTLE_SECONDS = 4.0

#: How long to wait for a freshly started Tally to answer its port.
START_TIMEOUT_SECONDS = 40.0

#: The licence screen's "Continue In Educational Mode" accelerator.
EDU_KEY = "t"


@dataclass(slots=True)
class Repair:
    """What was wrong and what was done about it."""

    healthy: bool
    actions: list[str] = field(default_factory=list)
    reason: str = ""

    def describe(self) -> str:
        if self.healthy and not self.actions:
            return "Tally was already answering"
        if self.healthy:
            return "recovered: " + "; ".join(self.actions)
        return self.reason or "could not recover Tally"


class Recovery:
    """Repairs a local Tally, at most once in a while.

    The cooldown is not politeness, it is a loop guard: a failing request that
    triggers a repair that fails and retries the request would restart Tally
    forever. One attempt per cooldown, and a request that fails again fails to
    the caller.
    """

    def __init__(
        self,
        config: TallyConfig,
        cooldown_seconds: float = 90.0,
        attempts: int = 4,
        sleep=time.sleep,  # type: ignore[no-untyped-def]
    ) -> None:
        self.config = config
        self.cooldown_seconds = cooldown_seconds
        self.attempts = attempts
        self._sleep = sleep
        self._last_attempt = 0.0
        #: Everything this object ever did, for the audit trail and for doctor.
        self.history: list[str] = []

    @property
    def cooling_down(self) -> bool:
        return (time.monotonic() - self._last_attempt) < self.cooldown_seconds

    async def repair(self, force: bool = False) -> Repair:
        if self.cooling_down and not force:
            return Repair(
                healthy=False,
                reason=(
                    "Tally is not answering and a repair was already tried a "
                    "moment ago. Open the Tally window and see what it is "
                    "showing."
                ),
            )
        self._last_attempt = time.monotonic()
        actions: list[str] = []

        if not await self.port_answers():
            installation = install.find_install()
            if installation is None:
                return Repair(
                    healthy=False,
                    actions=actions,
                    reason=(
                        "TallyPrime does not appear to be installed on this "
                        "machine, so there is nothing to restart."
                    ),
                )
            log.warning("Tally is not answering; restarting it")
            install.stop()
            if not install.start(installation):
                return Repair(
                    healthy=False,
                    actions=actions,
                    reason="TallyPrime would not start.",
                )
            actions.append("restarted TallyPrime")
            waited = 0.0
            while waited < START_TIMEOUT_SECONDS and not await self.port_answers():
                await asyncio.sleep(2)
                waited += 2

        if not await self.port_answers():
            return Repair(
                healthy=False,
                actions=actions,
                reason=(
                    f"{self.config.url} still does not answer. TallyPrime may be "
                    "showing a dialog that needs a person."
                ),
            )

        # The port can answer while Tally sits on the licence screen with no
        # company open, which looks like an empty but healthy company to every
        # read above this layer. That is worse than an error, so it counts as
        # unhealthy until a company is actually loaded.
        for attempt in range(1, self.attempts + 1):
            if await self.companies():
                self.history.extend(actions)
                return Repair(healthy=True, actions=actions)
            log.warning("no company loaded; clearing the licence screen (%d)", attempt)
            if not press_in_tally(EDU_KEY):
                break
            actions.append("cleared the licence screen")
            await asyncio.sleep(SETTLE_SECONDS)

        healthy = bool(await self.companies())
        self.history.extend(actions)
        return Repair(
            healthy=healthy,
            actions=actions,
            reason=(
                ""
                if healthy
                else (
                    "TallyPrime is answering but no company is open. Open the "
                    "Tally window: if it shows the License screen, choose "
                    "'Continue In Educational Mode'."
                )
            ),
        )

    async def port_answers(self) -> bool:
        try:
            banner = await TallyClient(
                TallyConfig(
                    host=self.config.host, port=self.config.port, timeout_seconds=5
                )
            ).banner()
        except Exception:  # noqa: BLE001 - "is it up" must never raise
            return False
        return "running" in banner.lower()

    async def companies(self) -> list[str]:
        try:
            return await TallyClient(
                TallyConfig(
                    host=self.config.host, port=self.config.port, timeout_seconds=15
                )
            ).list_companies()
        except Exception:  # noqa: BLE001
            return []


def press_in_tally(key: str) -> bool:
    """Send one keystroke to the Tally window. False when that is impossible.

    Only ever used for the licence screen, whose Educational-mode button changes
    no data. The window is matched on its title the same way Tier 2 matches it,
    so a browser tab called "tally-agent" is not a candidate.
    """
    try:
        import pyautogui  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]

        from tallyagent_agent.perception.screen import TALLY_TITLE
    except ImportError:
        log.info("pyautogui/pywin32 not installed; cannot clear the licence screen")
        return False

    handle: int | None = None

    def visit(candidate: int, _extra: object) -> None:
        nonlocal handle
        if handle is None and win32gui.IsWindowVisible(candidate):
            if TALLY_TITLE.search(win32gui.GetWindowText(candidate) or ""):
                handle = candidate

    win32gui.EnumWindows(visit, None)
    if handle is None:
        return False

    if win32gui.GetWindowPlacement(handle)[1] == win32con.SW_SHOWMINIMIZED:
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    try:
        from tallyagent_agent.fallback.window import raise_window

        raise_window(handle, "TallyPrime")
    except Exception:  # noqa: BLE001 - press anyway; the window may already be up
        pass
    time.sleep(0.8)
    pyautogui.press(key)
    return True
