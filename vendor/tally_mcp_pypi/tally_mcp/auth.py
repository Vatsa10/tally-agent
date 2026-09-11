"""User-based authentication with role-based access control.

Users are managed via CLI (``tally-mcp user add/list/revoke/...``).
Each user has a role (read/write/admin) and company scope.

If no users are configured, authentication is disabled (open access).
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from tally_mcp.company_config import _config_dir
from tally_mcp.user_manager import UserManager


class AccessDeniedError(Exception):
    """Raised when a user lacks the required access."""
    pass


def _get_user_manager() -> UserManager:
    """Get or create the global UserManager."""
    return UserManager(_config_dir())


class UserTokenVerifier(TokenVerifier):
    """Verify incoming Bearer tokens against the user registry."""

    def __init__(self) -> None:
        super().__init__()
        self._um = _get_user_manager()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an AccessToken when *token* is valid, else None."""
        if not token:
            return None
        user = self._um.validate_token(token)
        if not user:
            return None
        return AccessToken(
            token=token,
            client_id="tally-mcp-client",
            scopes=user.companies,
            claims={"role": user.role, "user": user.name},
        )


# Backward compat alias
ScopedTokenVerifier = UserTokenVerifier


def get_current_user() -> tuple[str, str, list[str]] | None:
    """Return (name, role, companies) for the current request's user.

    Returns None when no auth is configured (stdio mode / open access).
    """
    token_obj = get_access_token()
    if token_obj is None:
        return None
    name = token_obj.claims.get("user", "unknown") if token_obj.claims else "unknown"
    role = token_obj.claims.get("role", "read") if token_obj.claims else "read"
    return (name, role, list(token_obj.scopes))


def check_admin() -> None:
    """Verify the current request has admin access.

    Raises AccessDeniedError if auth is configured and the user
    doesn't have the admin role. No-op if auth is not configured.
    """
    token_obj = get_access_token()
    if token_obj is None:
        return
    role = token_obj.claims.get("role", "") if token_obj.claims else ""
    if role != "admin":
        raise AccessDeniedError(
            "Admin access required. This operation needs an admin user."
        )


def get_auth_provider() -> TokenVerifier | None:
    """Build an auth provider based on current state.

    Returns a UserTokenVerifier when users exist in users.json, else None.
    """
    um = _get_user_manager()
    registry = um.load()
    if registry.users:
        return UserTokenVerifier()
    return None
