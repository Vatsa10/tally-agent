"""Bringing TallyPrime from "installed" to "XML port answering", with no human
keystrokes in Tally.

Two attempts, in order:

**A. tally.ini.** Deterministic and preferred. The connectivity keys are known
(read off a real install - see docs/TALLY_INI_KEYS.md), so this is a backup, an
edit of three lines, and a restart.

**B. Tier 3 keystrokes.** Only if A cannot work: the keys are absent and we
refuse to guess their names, because a wrong key name silently does nothing,
which is the worst failure available here. B drives Tally's own Connectivity
screen, then diffs tally.ini before and after to *learn* the real key names, so
that machine never needs B again.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tallyagent_tally import install as install_mod
from tallyagent_tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

#: How long to wait for the port after a restart. Tally loads companies on the
#: way up, so this is slower than a process start.
PORT_WAIT_SECONDS = 45

#: Keystrokes for Tally's Connectivity screen. Tally is keyboard-first, so this
#: is a sequence of presses, not clicks. Deliberately not a blind script: the
#: fallback loop screenshots between steps and a human approves each change.
CONNECTIVITY_TASK = (
    "Turn on TallyPrime's server mode. Press F1 for Help, choose Settings, then "
    "Connectivity, then Client/Server configuration. Set 'TallyPrime acts as' to "
    "Server (or Both), set the port to {port}, set Enable ODBC to Yes, then "
    "accept the screen. Do not change anything else."
)


@dataclass(slots=True)
class EnableResult:
    ok: bool = False
    attempt: str = ""  # ini | tier3 | already | none
    used_tier3: bool = False
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    backup: str = ""
    learned_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "attempt": self.attempt,
            "used_tier3": self.used_tier3,
            "changes": {k: list(v) for k, v in self.changes.items()},
            "backup": self.backup,
            "learned_keys": {k: list(v) for k, v in self.learned_keys.items()},
        }


async def port_answers(host: str, port: int, timeout: float = 3.0) -> bool:
    """Does Tally's XML interface answer? The only definition of success here."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"http://{host}:{port}")
        return "tally" in response.content.decode("utf-8", "replace").lower()
    except httpx.RequestError:
        return False


async def wait_for_port(
    host: str, port: int, seconds: int = PORT_WAIT_SECONDS
) -> bool:
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        if await port_answers(host, port):
            return True
        await asyncio.sleep(2)
    return False


async def enable_tally_server(
    ctx: ToolContext,
    port: int = 9000,
    host: str = "127.0.0.1",
    approve: Any = None,
    install_dir: str | Path | None = None,
    allow_tier3: bool = True,
) -> ToolResult:
    """Make Tally listen on ``port``. Returns what was done and whether it worked."""
    result = EnableResult()

    if await port_answers(host, port):
        result.ok, result.attempt = True, "already"
        result.message = (
            f"TallyPrime is already answering on {host}:{port}; nothing to do."
        )
        return ToolResult(message=result.message, data=result.as_dict())

    installation = install_mod.find_install(install_dir)
    if installation is None:
        result.message = (
            "No TallyPrime installation found. Looked at the running process, "
            f"{', '.join(install_mod.CANDIDATE_DIRS)}, and the uninstall registry "
            "keys. Install TallyPrime, or pass the install directory explicitly."
        )
        return ToolResult(message=result.message, data=result.as_dict())

    log.info("found TallyPrime %s at %s", installation.version, installation.install_dir)

    # --- Attempt A: the file ---
    before_text = (
        installation.ini.read_text(encoding="utf-8", errors="replace")
        if installation.ini.exists()
        else ""
    )
    connectivity = install_mod.read_connectivity(installation.ini)
    keys_present = {
        install_mod.KEY_CLIENT_SERVER.lower(),
        install_mod.KEY_PORT.lower(),
    } <= set(connectivity.raw)

    if installation.ini.exists() and keys_present:
        backup, changes = install_mod.set_connectivity(
            installation.ini, port=port, client_server="Both", odbc=True
        )
        result.attempt, result.backup, result.changes = "ini", str(backup), changes
        log.info("tally.ini updated: %s", changes or "already correct")

        if install_mod.restart(installation) and await wait_for_port(host, port):
            result.ok = True
            described = (
                ", ".join(f"{k}: {old} -> {new}" for k, (old, new) in changes.items())
                or "already correct"
            )
            result.message = (
                f"TallyPrime is answering on {host}:{port}. Set via tally.ini "
                f"({described}). Backup: {backup.name}"
            )
            return ToolResult(message=result.message, data=result.as_dict())

        log.warning("tally.ini edit did not bring the port up; falling back")

    # --- Attempt B: the UI ---
    if not allow_tier3:
        result.message = (
            "tally.ini does not carry the connectivity keys and the Tier 3 "
            "fallback is disabled, so server mode cannot be enabled "
            "automatically. Enable it once by hand (F1 > Settings > "
            "Connectivity) and tallyagent will learn the key names."
        )
        return ToolResult(message=result.message, data=result.as_dict())

    tier3 = await _drive_connectivity_screen(ctx, port, approve)
    result.used_tier3 = True
    result.attempt = "tier3"
    if tier3 is not None and not tier3.completed:
        result.message = f"Tier 3 could not finish: {tier3.stopped_reason}"
        return ToolResult(message=result.message, data=result.as_dict())

    if not install_mod.restart(installation):
        result.message = "Tally would not restart after the Connectivity change."
        return ToolResult(message=result.message, data=result.as_dict())

    result.ok = await wait_for_port(host, port)

    # Whatever the UI wrote is the truth about this version's key names.
    after_text = (
        installation.ini.read_text(encoding="utf-8", errors="replace")
        if installation.ini.exists()
        else ""
    )
    result.learned_keys = install_mod.diff_ini(before_text, after_text)
    if result.learned_keys:
        log.info("learned tally.ini keys: %s", result.learned_keys)

    learned = (
        " Learned tally.ini keys: "
        + ", ".join(f"{k}={new}" for k, (_old, new) in result.learned_keys.items())
        + " - record them in docs/TALLY_INI_KEYS.md so this never needs the UI again."
        if result.learned_keys
        else ""
    )
    result.message = (
        f"TallyPrime is answering on {host}:{port} after driving the Connectivity "
        f"screen.{learned}"
        if result.ok
        else f"Drove the Connectivity screen but {host}:{port} still does not answer. "
        "Check whether Windows Firewall is prompting - an OS dialog must be "
        "answered by you, not by the agent."
    )
    return ToolResult(message=result.message, data=result.as_dict())


async def _drive_connectivity_screen(ctx: ToolContext, port: int, approve: Any):  # type: ignore[no-untyped-def]
    """Hand the Connectivity screen to the Tier 3 fallback.

    Returns None when no fallback is wired, which the caller treats as "try the
    restart anyway" - useful on a machine where the file edit already worked but
    the port needed a bounce.
    """
    runner = getattr(ctx, "fallback", None)
    if runner is None:
        log.info("no Tier 3 runner wired; skipping the UI attempt")
        return None
    runner.approve = approve or runner.approve
    return await runner.run(CONNECTIVITY_TASK.format(port=port))
