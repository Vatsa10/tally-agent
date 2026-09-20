"""Read a local ``.env`` into the environment, once, at startup.

The rule this looks like it breaks is "secrets come from the environment or the
OS keyring, never from config files". It does not: a ``.env`` *is* how a
developer sets environment variables on Windows, it is gitignored, and nothing
here ever reads a secret out of ``config.toml``.

It exists because of what happened without it. The key sat in ``.env``, the
agent started, found nothing, fell back to the deterministic mock exactly as
designed - and answered every question with a canned sentence. The warning was
in the log, three lines above the prompt, and it was missed repeatedly by the
person who had written the fallback. A user would have concluded the product
was stupid rather than unconfigured.

A variable already set always wins, so exporting a key in the shell or in CI
overrides the file rather than the other way round.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: Only these. A .env may hold anything; the agent has no business importing a
#: developer's unrelated environment into the process it runs the books with.
KNOWN_KEYS = (
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "TALLY_PASSWORD",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_ACCESS_TOKEN",
)


def parse(text: str) -> dict[str, str]:
    """KEY=VALUE pairs, with the usual conveniences and nothing more.

    Comments, blank lines, a leading ``export``, and values in matching quotes.
    No interpolation and no multi-line values: this finds an API key, it does
    not reimplement a shell.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load(path: str | Path = ".env") -> list[str]:
    """Apply a local .env. Returns the names actually set, for the log."""
    file = Path(path)
    if not file.is_file():
        return []

    applied: list[str] = []
    try:
        found = parse(file.read_text(encoding="utf-8"))
    except OSError as exc:  # noqa: BLE001 - unreadable .env is not fatal
        log.debug("could not read %s: %s", file, exc)
        return []

    for key, value in found.items():
        if key not in KNOWN_KEYS or os.environ.get(key):
            continue
        os.environ[key] = value
        applied.append(key)
    return applied
