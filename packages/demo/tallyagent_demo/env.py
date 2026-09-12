"""Bridge a developer's ``.env`` into the environment, for the demo build only.

The product reads secrets from the environment or the OS keyring and never from
a file - see ``tallyagent_llm.router.api_key_for``. That rule is not being
relaxed here: this only copies a local ``.env`` into ``os.environ`` so the same
lookup finds it, and only the demo build calls it. Nothing in the daemon does.

A variable that is already set always wins, so exporting a key in the shell - or
in CI - overrides the file rather than being silently overridden by it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Lines that set nothing. A ``.env`` full of comments is normal.
_IGNORED_PREFIXES = ("#", ";")


def parse(text: str) -> dict[str, str]:
    """The KEY=VALUE pairs in a ``.env``, with the usual conveniences.

    Handles comments, blank lines, a leading ``export``, and values wrapped in
    matching single or double quotes. Deliberately does not handle variable
    interpolation or multi-line values: this file exists to find two API keys,
    not to reimplement a shell.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(_IGNORED_PREFIXES):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: str | Path = ".env") -> list[str]:
    """Load ``path`` into ``os.environ``. Returns the names actually set.

    Missing file is not an error - the keys may well be exported already.
    """
    file = Path(path)
    if not file.is_file():
        return []

    applied: list[str] = []
    for key, value in parse(file.read_text(encoding="utf-8")).items():
        if os.environ.get(key):
            # Already set: a real environment variable outranks a file.
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def require(name: str, what: str) -> str:
    """A key, or an error that says which key and where to put it."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set, so {what} cannot run. Put it in .env at the "
            f"repo root or export it in your shell."
        )
    return value
