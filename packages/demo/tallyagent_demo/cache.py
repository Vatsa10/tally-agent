"""A content-addressed store, so re-rendering the video costs no API calls.

Every artefact is keyed on the full set of inputs that could change its bytes -
the text, the voice, the rate, the sample rate. Edit one sentence in the script
and exactly one line is re-synthesised; change nothing and the whole build is
free. That is the difference between a demo you iterate on and a demo you record
once and live with.

Each entry is two files: the payload, and a ``.json`` sidecar holding the
request and response that produced it, so months later it is still answerable
what was sent to whom.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def key_for(payload: dict[str, Any]) -> str:
    """A stable sha256 over a request. Key order must not change the answer."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def file_key(path: Path, salt: str = "") -> str:
    """A key over a file's *contents*, for artefacts derived from other files."""
    digest = hashlib.sha256()
    digest.update(salt.encode("utf-8"))
    digest.update(path.read_bytes())
    return digest.hexdigest()[:32]


class Cache:
    """Files under one directory, addressed by key and extension."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        #: Counts what actually went out, so a test can assert "zero calls".
        self.hits = 0
        self.misses = 0

    def path(self, key: str, suffix: str) -> Path:
        return self.root / f"{key}{suffix}"

    def has(self, key: str, suffix: str) -> bool:
        present = self.path(key, suffix).is_file()
        if present:
            self.hits += 1
        else:
            self.misses += 1
        return present

    def put(
        self,
        key: str,
        suffix: str,
        data: bytes,
        sidecar: dict[str, Any] | None = None,
    ) -> Path:
        target = self.path(key, suffix)
        target.write_bytes(data)
        if sidecar is not None:
            self.path(key, ".json").write_text(
                json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8"
            )
        return target

    def sidecar(self, key: str) -> dict[str, Any]:
        path = self.path(key, ".json")
        if not path.is_file():
            return {}
        loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    def put_json(self, key: str, suffix: str, value: Any) -> Path:
        target = self.path(key, suffix)
        target.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return target

    def get_json(self, key: str, suffix: str) -> Any:
        return json.loads(self.path(key, suffix).read_text(encoding="utf-8"))
