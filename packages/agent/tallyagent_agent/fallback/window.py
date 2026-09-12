"""Get TallyPrime on screen and in front before Tier 3 touches it.

Keystrokes go to whatever window has focus. If that is not Tally, an approved
`Alt+D` lands somewhere it was never meant to - so this runs first, every time,
and the fallback refuses to act if it cannot get Tally in front.

Three states are handled, because all three happen on a student install:
Tally is not running, Tally is running but minimised or behind something, and
Tally is up but sitting on the licence screen with no company loaded.
"""

from __future__ import annotations

import logging
import time

from tallyagent_agent.perception.screen import WindowBounds, find_tally_window

log = logging.getLogger(__name__)

#: How long to wait for a freshly started Tally to put a window up.
START_TIMEOUT_SECONDS = 40


def focus(bounds: WindowBounds) -> bool:
    """Bring a window to the front. False when Windows refuses, as it may."""
    handle = _handle_for(bounds.title)
    if handle is None:
        return False
    return raise_window(handle, bounds.title)


def raise_window(handle: int, title: str = "") -> bool:
    """Actually bring a window forward, against Windows' wishes.

    A process that does not own the foreground is not allowed to steal it, so a
    bare ``SetForegroundWindow`` from a background script simply fails. Two
    documented ways round it, tried in order: borrow the foreground window's
    input queue with ``AttachThreadInput``, or press and release Alt, which
    Windows counts as input activity and which unlocks the call.

    This is not a trick to be clever with. Tier 3 sends keystrokes, and a
    keystroke lands wherever focus is - so if none of this works, the caller
    must send nothing at all.
    """
    import win32con  # type: ignore[import-not-found]
    import win32gui  # type: ignore[import-not-found]
    import win32process  # type: ignore[import-not-found]

    def settled() -> bool:
        time.sleep(0.4)
        return _foreground() == handle

    try:
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(handle)
        if settled():
            return True
    except Exception:  # noqa: BLE001 - the refusal is the normal case here
        pass

    try:
        import win32api  # type: ignore[import-not-found]

        current = win32gui.GetForegroundWindow()
        ours = win32process.GetWindowThreadProcessId(current)[0]
        theirs = win32process.GetWindowThreadProcessId(handle)[0]
        win32process.AttachThreadInput(ours, theirs, True)
        try:
            win32gui.BringWindowToTop(handle)
            win32gui.SetForegroundWindow(handle)
        finally:
            win32process.AttachThreadInput(ours, theirs, False)
        if settled():
            return True

        # Alt counts as input activity, which lifts the foreground lock.
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(handle)
        if settled():
            return True
    except Exception:  # noqa: BLE001
        log.debug("could not raise %s", title or handle)

    return False


def _handle_for(title: str) -> int | None:
    try:
        import win32gui  # type: ignore[import-not-found]
    except ImportError:
        return None

    found: list[int] = []

    def visit(handle: int, _extra: object) -> None:
        if win32gui.IsWindowVisible(handle) and win32gui.GetWindowText(handle) == title:
            found.append(handle)

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def _foreground() -> int | None:
    try:
        import win32gui  # type: ignore[import-not-found]

        return int(win32gui.GetForegroundWindow())
    except Exception:  # noqa: BLE001
        return None


def start_tally() -> bool:
    """Launch TallyPrime. False when it is not installed or will not start."""
    from tallyagent_tally import install

    installation = install.find_install()
    if installation is None:
        return False
    return bool(install.start(installation))


def ensure_visible(
    locate=find_tally_window,  # type: ignore[no-untyped-def]
    launch=start_tally,  # type: ignore[no-untyped-def]
    raise_window=focus,  # type: ignore[no-untyped-def]
    sleep=time.sleep,  # type: ignore[no-untyped-def]
) -> tuple[WindowBounds | None, str]:
    """Tally on screen and in front, or a reason why not.

    Returns the window and an empty reason on success. Everything is injected
    so the decision sequence can be tested without a desktop.
    """
    bounds = locate()
    if bounds is None:
        log.info("TallyPrime is not on screen; starting it")
        if not launch():
            return None, "TallyPrime is not installed, or would not start"
        waited = 0.0
        while waited < START_TIMEOUT_SECONDS:
            sleep(2)
            waited += 2
            bounds = locate()
            if bounds is not None:
                break
        if bounds is None:
            return None, "TallyPrime was started but never put a window up"

    if not raise_window(bounds):
        return None, (
            "TallyPrime is open but could not be brought to the front. "
            "Keystrokes would go to whatever is in front instead, so nothing "
            "was sent. Click the Tally window and try again."
        )
    return bounds, ""
