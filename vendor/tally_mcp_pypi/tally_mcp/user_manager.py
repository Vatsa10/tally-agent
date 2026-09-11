"""User-based authentication with role-based access control.

Users are stored in ``users.json`` in the config directory.
Each user has a name, role (read/write/admin), company scope, and a token.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

VALID_ROLES = {"read", "write", "admin"}

_ROLE_PREFIX = {
    "read": "tmcp_rd_",
    "write": "tmcp_wr_",
    "admin": "tmcp_adm_",
}


class UserRecord(BaseModel):
    """A single registered user."""

    name: str
    role: str = Field(description="read, write, or admin")
    companies: list[str] = Field(description='["*"] for admin, or specific company names')
    token: str
    created: str
    last_used: str | None = None


class UserRegistry(BaseModel):
    """All registered users, keyed by name."""

    users: dict[str, UserRecord] = Field(default_factory=dict)


class UserManager:
    """CRUD operations on the user registry file."""

    def __init__(self, config_dir: Path):
        self.path = config_dir / "users.json"
        self._registry: UserRegistry | None = None
        self._mtime: float = 0

    def load(self) -> UserRegistry:
        """Load registry from disk, re-reading if file changed since last load."""
        mtime = self.path.stat().st_mtime if self.path.exists() else 0
        if self._registry is not None and mtime == self._mtime:
            return self._registry
        self._mtime = mtime
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._registry = UserRegistry(**data)
        else:
            self._registry = UserRegistry()
        return self._registry

    def save(self) -> None:
        """Persist registry to disk."""
        reg = self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(reg.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add_user(self, name: str, role: str, companies: list[str]) -> str:
        """Add a new user and return their token.

        Raises ValueError if name already exists or role is invalid.
        """
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}")
        reg = self.load()
        if name in reg.users:
            raise ValueError(f"User '{name}' already exists. Use 'cycle' to regenerate their token.")
        prefix = _ROLE_PREFIX[role]
        token = f"{prefix}{secrets.token_hex(16)}"
        reg.users[name] = UserRecord(
            name=name,
            role=role,
            companies=companies,
            token=token,
            created=datetime.now().isoformat(timespec="seconds"),
        )
        self.save()
        return token

    def get_by_token(self, token: str) -> UserRecord | None:
        """Find a user by their token. O(n) scan — fine for <20 users."""
        if not token:
            return None
        reg = self.load()
        for user in reg.users.values():
            if user.token == token:
                return user
        return None

    def get_by_name(self, name: str) -> UserRecord | None:
        """Find a user by name."""
        reg = self.load()
        return reg.users.get(name)

    def validate_token(self, token: str) -> UserRecord | None:
        """Validate token, update last_used, return user record or None."""
        user = self.get_by_token(token)
        if user:
            user.last_used = datetime.now().isoformat(timespec="seconds")
            self.save()
        return user

    def grant_company(self, name: str, company: str) -> bool:
        """Add a company to a user's scope. Returns True if added."""
        user = self.get_by_name(name)
        if not user:
            return False
        if "*" in user.companies:
            return False  # wildcard already has access to everything
        if company in user.companies:
            return False  # already has access
        user.companies.append(company)
        self.save()
        return True

    def revoke_user(self, name: str) -> bool:
        """Remove a user entirely. Returns True if found and removed."""
        reg = self.load()
        if name in reg.users:
            del reg.users[name]
            self.save()
            return True
        return False

    def cycle_token(self, name: str) -> str | None:
        """Generate a new token for a user, preserving role and companies."""
        user = self.get_by_name(name)
        if not user:
            return None
        prefix = _ROLE_PREFIX[user.role]
        new_token = f"{prefix}{secrets.token_hex(16)}"
        user.token = new_token
        self.save()
        return new_token

    def revoke_all(self) -> int:
        """Remove all users. Returns count removed."""
        reg = self.load()
        count = len(reg.users)
        reg.users = {}
        self.save()
        return count

    def list_users(self) -> list[dict]:
        """List all users with redacted token hints."""
        reg = self.load()
        result = []
        for user in reg.users.values():
            hint = user.token[:12] + "..." if len(user.token) > 12 else user.token
            result.append({
                "name": user.name,
                "role": user.role,
                "companies": user.companies,
                "token_hint": hint,
                "created": user.created,
                "last_used": user.last_used,
            })
        return result
