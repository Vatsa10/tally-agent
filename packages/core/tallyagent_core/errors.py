"""Typed error taxonomy.

Shape borrowed from vendor/taxor-tally-mcp/pkg/tally/errors.go: connection,
remote-side and validation failures are distinct types so callers can branch on
"retry", "fix the data" and "tell the user Tally is down" without string matching.
"""

from __future__ import annotations


class TallyAgentError(Exception):
    """Base for everything this system raises deliberately."""


class ConnectionError_(TallyAgentError):
    """Tally is unreachable at the configured address."""

    def __init__(self, address: str, details: str) -> None:
        self.address = address
        self.details = details
        super().__init__(f"Cannot connect to Tally at {address}: {details}")


class TallyError(TallyAgentError):
    """Tally answered, but rejected the request.

    ``line_errors`` holds the parsed <LINEERROR> texts when the failure came
    from an import; Tally puts the useful detail there and nowhere else.
    """

    def __init__(self, message: str, line_errors: list[str] | None = None) -> None:
        self.line_errors = line_errors or []
        super().__init__(message)


class ValidationError(TallyAgentError):
    """A deterministic pre-write rule failed."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"Validation error in {field}: {message}")


class PolicyError(TallyAgentError):
    """Action blocked by policy (e.g. Tier 3 fallback disabled)."""


class NotConfiguredError(TallyAgentError):
    """A required optional dependency or config value is missing."""
