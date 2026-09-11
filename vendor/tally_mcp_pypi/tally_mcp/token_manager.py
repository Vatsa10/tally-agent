"""Scoped token registry for per-company access control.

Tokens are stored in ``auth_tokens.json`` in the config directory.
Each token grants access to specific companies (or all via ``["*"]``).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class TokenEntry(BaseModel):
    """A single auth token and its scope."""

    name: str
    companies: list[str] = Field(description='["*"] for admin, or specific company names')
    created: str
    last_used: str | None = None


class TokenRegistry(BaseModel):
    """All registered tokens."""

    tokens: dict[str, TokenEntry] = Field(default_factory=dict)


class TokenManager:
    """CRUD operations on the token registry file."""

    def __init__(self, config_dir: Path):
        self.path = config_dir / "auth_tokens.json"
        self._registry: TokenRegistry | None = None

    def load(self) -> TokenRegistry:
        """Load registry from disk (or return empty)."""
        if self._registry is not None:
            return self._registry
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._registry = TokenRegistry(**data)
        else:
            self._registry = TokenRegistry()
        return self._registry

    def save(self) -> None:
        """Persist registry to disk."""
        reg = self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(reg.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def generate_admin_token(self, name: str = "admin") -> str:
        """Generate a new admin token (access to all companies)."""
        reg = self.load()
        token = f"tmcp_adm_{secrets.token_hex(6)}"
        reg.tokens[token] = TokenEntry(
            name=name,
            companies=["*"],
            created=datetime.now().isoformat(timespec="seconds"),
        )
        self.save()
        return token

    def generate_company_token(self, company: str) -> str:
        """Generate a token scoped to one company."""
        reg = self.load()
        token = f"tmcp_co_{secrets.token_hex(6)}"
        reg.tokens[token] = TokenEntry(
            name=company,
            companies=[company],
            created=datetime.now().isoformat(timespec="seconds"),
        )
        self.save()
        return token

    def validate(self, token: str) -> TokenEntry | None:
        """Validate token and return its entry (or None)."""
        if not token:
            return None
        reg = self.load()
        entry = reg.tokens.get(token)
        if entry:
            entry.last_used = datetime.now().isoformat(timespec="seconds")
            self.save()
        return entry

    def can_access(self, token: str, company: str) -> bool:
        """Check if token has access to a specific company."""
        entry = self.validate(token)
        if not entry:
            return False
        return "*" in entry.companies or company in entry.companies

    def revoke(self, token: str) -> bool:
        """Revoke a token. Returns True if found and removed."""
        reg = self.load()
        if token in reg.tokens:
            del reg.tokens[token]
            self.save()
            return True
        return False

    def cycle_token(self, old_token: str) -> str | None:
        """Replace a token with a new one, same scope. Returns new token."""
        entry = self.validate(old_token)
        if not entry:
            return None
        name = entry.name
        companies = list(entry.companies)
        self.revoke(old_token)
        if "*" in companies:
            return self.generate_admin_token(name=name)
        else:
            return self.generate_company_token(companies[0])

    def find_by_prefix(self, prefix: str) -> str | None:
        """Find a token by its prefix (first N characters)."""
        reg = self.load()
        for token in reg.tokens:
            if token.startswith(prefix):
                return token
        return None

    def list_tokens(self) -> list[dict]:
        """List all tokens (redacted) with their scopes."""
        reg = self.load()
        result = []
        for token, entry in reg.tokens.items():
            hint = token[:8] + "..." + token[-4:] if len(token) > 12 else token
            result.append({
                "token_hint": hint,
                "name": entry.name,
                "companies": entry.companies,
                "created": entry.created,
                "last_used": entry.last_used,
            })
        return result
