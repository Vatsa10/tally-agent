"""Get Tally back to a working state after a crash, a hang, or a restart.

    uv run python scripts/recover_tally.py

Three things go wrong repeatedly on an Educational install, and this handles all
of them:

1. Tally has crashed or is hung (a malformed TDL does both). Restart it.
2. Tally is up but stopped at the licence screen, because it cannot reach the
   licence gateway on port 9999. Press T for Continue In Educational Mode.
3. Tally is past the licence screen but no company is loaded, so everything is
   invisible over XML even with SVCURRENTCOMPANY set. Clearing the licence
   screen lets tally.ini's ``Load=<number>`` do its job.

Exits 0 when a company is loaded and the port answers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tallyagent_tally import install
from tallyagent_tally.client import TallyClient, TallyConfig

#: Tally needs a moment after each keypress before the next check is meaningful.
SETTLE_SECONDS = 4


async def port_answers(host: str, port: int) -> bool:
    try:
        return "running" in (
            await TallyClient(
                TallyConfig(host=host, port=port, timeout_seconds=5)
            ).banner()
        ).lower()
    except Exception:  # noqa: BLE001 - "is it up" must never raise
        return False


async def loaded_companies(host: str, port: int) -> list[str]:
    try:
        return await TallyClient(
            TallyConfig(host=host, port=port, timeout_seconds=15)
        ).list_companies()
    except Exception:  # noqa: BLE001
        return []


def press(key: str) -> bool:
    """Send one keystroke to the Tally window.

    Read-only in effect: the licence screen's Educational-mode button changes no
    data. Anything that could is Tier 3's business, not this script's.
    """
    try:
        import pyautogui  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
    except ImportError:
        print("  (pyautogui/pywin32 not installed; cannot clear the licence screen)")
        return False

    handle = None

    def visit(candidate: int, _extra: object) -> None:
        nonlocal handle
        if handle is None and win32gui.IsWindowVisible(candidate):
            if "TallyPrime" in (win32gui.GetWindowText(candidate) or ""):
                handle = candidate

    win32gui.EnumWindows(visit, None)
    if handle is None:
        return False
    win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(handle)
    except Exception:  # noqa: BLE001 - Windows refuses this sometimes; press anyway
        pass
    import time

    time.sleep(0.8)
    pyautogui.press(key)
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument(
        "--attempts", type=int, default=4, help="How many times to clear the licence screen."
    )
    args = parser.parse_args()

    installation = install.find_install()
    if installation is None:
        print("No TallyPrime installation found.", file=sys.stderr)
        return 2

    if not await port_answers(args.host, args.port):
        print("Port is closed - restarting TallyPrime...")
        install.stop()
        if not install.start(installation):
            print("Tally would not start.", file=sys.stderr)
            return 1
        for _ in range(20):
            if await port_answers(args.host, args.port):
                break
            await asyncio.sleep(2)

    if not await port_answers(args.host, args.port):
        print(f"{args.host}:{args.port} still does not answer.", file=sys.stderr)
        return 1
    print(f"Port {args.host}:{args.port} is answering.")

    for attempt in range(1, args.attempts + 1):
        companies = await loaded_companies(args.host, args.port)
        if companies:
            print(f"Companies loaded: {', '.join(companies)}")
            return 0
        print(f"No company loaded - clearing the licence screen (attempt {attempt})")
        if not press("t"):
            break
        await asyncio.sleep(SETTLE_SECONDS)

    print(
        "Tally is up but no company is loaded. Open the Tally window: if it is "
        "showing the License screen, choose 'T: Continue In Educational Mode'.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
