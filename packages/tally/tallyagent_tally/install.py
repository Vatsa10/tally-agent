"""Finding, reading, configuring and restarting a local TallyPrime install.

This is the only module that touches the Tally installation rather than its XML
port. It exists so the agent can bring Tally from "installed, server mode off"
to "XML port answering" without a human pressing keys in Tally.

The ``tally.ini`` key names here are not guesses: they were read from a real
TallyPrime 1.1.7.1 install. See docs/TALLY_INI_KEYS.md.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Default install locations, most likely first.
CANDIDATE_DIRS = (
    r"C:\Program Files\TallyPrime",
    r"C:\Program Files (x86)\TallyPrime",
    r"C:\Tally\TallyPrime",
    r"C:\Program Files\Tally\TallyPrime",
)

EXE_NAME = "tally.exe"
INI_NAME = "tally.ini"

#: The connectivity keys, verified against TallyPrime 1.1.7.1.
#: ``Client Server`` takes Both / Server / Client / None.
KEY_CLIENT_SERVER = "Client Server"
KEY_PORT = "ServerPort"
KEY_ODBC = "Enable ODBC Server"
KEY_DATA = "Data"

#: Tally puts its port in the window title once server mode is on:
#: "TallyPrime:9000". A cheap, reliable way to confirm without the port.
WINDOW_TITLE = re.compile(r"TallyPrime(?::(\d+))?", re.I)


@dataclass(slots=True)
class TallyInstall:
    install_dir: Path
    exe: Path
    ini: Path
    data_dir: Path | None = None
    version: str = ""

    @property
    def exists(self) -> bool:
        return self.exe.exists()


@dataclass(slots=True)
class Connectivity:
    """What tally.ini currently says about the XML interface."""

    client_server: str = ""
    port: int = 0
    odbc: bool = False
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def server_mode(self) -> bool:
        return self.client_server.strip().lower() in ("both", "server")

    def summary(self) -> str:
        return (
            f"Client Server={self.client_server or '(unset)'}, "
            f"ServerPort={self.port or '(unset)'}, "
            f"ODBC={'Yes' if self.odbc else 'No'}"
        )


def find_install(explicit: str | Path | None = None) -> TallyInstall | None:
    """Locate TallyPrime: the running process first, then the usual directories.

    The running process is authoritative - a machine can have two installs, and
    the one answering the port is the one we must configure.
    """
    if explicit:
        directory = Path(explicit)
        if (directory / EXE_NAME).exists():
            return _describe(directory)

    running = running_install_dir()
    if running is not None:
        return _describe(running)

    for candidate in CANDIDATE_DIRS:
        directory = Path(candidate)
        if (directory / EXE_NAME).exists():
            return _describe(directory)

    registry = _registry_install_dir()
    if registry is not None and (registry / EXE_NAME).exists():
        return _describe(registry)
    return None


def _describe(directory: Path) -> TallyInstall:
    install = TallyInstall(
        install_dir=directory,
        exe=directory / EXE_NAME,
        ini=directory / INI_NAME,
        version=exe_version(directory / EXE_NAME),
    )
    connectivity = read_connectivity(install.ini)
    data = connectivity.raw.get(KEY_DATA.lower())
    if data:
        install.data_dir = Path(data)
    return install


def running_install_dir() -> Path | None:
    """The directory of the running tally.exe, via psutil if available."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        log.debug("psutil not installed; cannot locate the running Tally")
        return None
    for process in psutil.process_iter(["name", "exe"]):
        name = (process.info.get("name") or "").lower()
        if name == EXE_NAME:
            exe = process.info.get("exe")
            if exe:
                return Path(exe).parent
    return None


def _registry_install_dir() -> Path | None:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None
    roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in roots:
        try:
            with winreg.OpenKey(root, path) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, index)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        try:
                            display = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        except OSError:
                            continue
                        if "tallyprime" not in str(display).lower():
                            continue
                        try:
                            location = winreg.QueryValueEx(subkey, "InstallLocation")[0]
                        except OSError:
                            continue
                        if location:
                            return Path(location)
        except OSError:
            continue
    return None


def exe_version(exe: Path) -> str:
    """Read the file version. TallyPrime does not report it over XML."""
    if not exe.exists() or os.name != "nt":
        return ""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Item '{exe}').VersionInfo.ProductVersion",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# --- tally.ini --------------------------------------------------------------


def read_connectivity(ini: Path) -> Connectivity:
    """Parse the connectivity keys out of tally.ini.

    Not ``configparser``: tally.ini uses ``;;`` comments, repeated keys and
    values containing ``:`` and ``\\``, which configparser mangles.
    """
    if not ini.exists():
        return Connectivity()
    raw: dict[str, str] = {}
    for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "[")):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        raw[key.strip().lower()] = value.strip()

    port_text = raw.get(KEY_PORT.lower(), "")
    return Connectivity(
        client_server=raw.get(KEY_CLIENT_SERVER.lower(), ""),
        port=int(port_text) if port_text.isdigit() else 0,
        odbc=raw.get(KEY_ODBC.lower(), "").strip().lower() == "yes",
        raw=raw,
    )


def backup_ini(ini: Path) -> Path:
    """Copy tally.ini aside before editing. Never edit in place without this."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = ini.with_suffix(f".ini.tallyagent-{stamp}.bak")
    shutil.copy2(ini, backup)
    return backup


def set_connectivity(
    ini: Path, port: int = 9000, client_server: str = "Both", odbc: bool = True
) -> tuple[Path, dict[str, tuple[str, str]]]:
    """Write the connectivity keys, preserving everything else byte for byte.

    Returns ``(backup_path, changes)`` where changes maps key -> (before, after).
    Keys that are absent are appended under the ``[TALLY]`` section rather than
    invented elsewhere in the file.
    """
    backup = backup_ini(ini)
    original = ini.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines(keepends=True)

    wanted = {
        KEY_CLIENT_SERVER.lower(): (KEY_CLIENT_SERVER, client_server),
        KEY_PORT.lower(): (KEY_PORT, str(port)),
        KEY_ODBC.lower(): (KEY_ODBC, "Yes" if odbc else "No"),
    }
    changes: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "[")) or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        lowered = key.strip().lower()
        if lowered not in wanted:
            continue
        proper, target = wanted[lowered]
        seen.add(lowered)
        before = value.strip()
        if before != target:
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = f"{proper}={target}{ending}"
            changes[proper] = (before, target)

    missing = [wanted[key] for key in wanted if key not in seen]
    if missing:
        block = ["\r\n;; added by tallyagent enable_tally_server\r\n"]
        for proper, target in missing:
            block.append(f"{proper}={target}\r\n")
            changes[proper] = ("(absent)", target)
        lines.extend(block)

    if changes:
        ini.write_text("".join(lines), encoding="utf-8")
    return backup, changes


def diff_ini(before_text: str, after_text: str) -> dict[str, tuple[str, str]]:
    """Which keys changed between two tally.ini contents.

    Used to *learn* the real key names after a Tier 3 UI run configured Tally
    for us, so the next machine can be configured by file edit alone.
    """

    def parse(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith((";", "[")) or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            out[key.strip()] = value.strip()
        return out

    before, after = parse(before_text), parse(after_text)
    changes: dict[str, tuple[str, str]] = {}
    for key, value in after.items():
        if before.get(key) != value:
            changes[key] = (before.get(key, "(absent)"), value)
    for key, value in before.items():
        if key not in after:
            changes[key] = (value, "(removed)")
    return changes


# --- process control --------------------------------------------------------


def tally_windows() -> list[tuple[int, str]]:
    """Running Tally processes as ``(pid, window title)``.

    The title carries the port once server mode is on ("TallyPrime:9000"),
    which confirms configuration without touching the port.
    """
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process tally -ErrorAction SilentlyContinue | "
                "ForEach-Object { \"$($_.Id)`t$($_.MainWindowTitle)\" }",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        pid, _, title = line.partition("\t")
        if pid.strip().isdigit():
            found.append((int(pid.strip()), title.strip()))
    return found


def configured_port_from_title() -> int | None:
    for _pid, title in tally_windows():
        match = WINDOW_TITLE.search(title)
        if match and match.group(1):
            return int(match.group(1))
    return None


def is_running() -> bool:
    return bool(tally_windows())


def stop(timeout: float = 30.0) -> bool:
    """Close Tally gracefully, then terminate if it will not go.

    Graceful first because Tally flushes company data on close; killing it with
    a company loaded is how a student ends up with a corrupt data folder.
    """
    if os.name != "nt":
        return False
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process tally -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.CloseMainWindow() | Out-Null }",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(1)

    log.warning("Tally did not close gracefully; terminating")
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process tally -ErrorAction SilentlyContinue | Stop-Process -Force",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    time.sleep(2)
    return not is_running()


def start(install: TallyInstall, timeout: float = 60.0) -> bool:
    """Launch Tally and wait for its window.

    ``Start-Process`` rather than a bare subprocess: Tally is a GUI app and must
    attach to the user's desktop session, not this shell's.
    """
    if os.name != "nt":
        return False
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Start-Process -FilePath '{install.exe}' "
            f"-WorkingDirectory '{install.install_dir}'",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_running():
            return True
        time.sleep(1)
    return False


def restart(install: TallyInstall) -> bool:
    stop()
    return start(install)
