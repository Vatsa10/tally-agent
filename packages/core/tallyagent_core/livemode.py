"""Live-mode guard rails.

Everything through Stage 13 ran against a fake Tally, where a mistake costs
nothing. Against a real installation a mistake writes to somebody's books, so
live mode adds two constraints that hold *in code*, not in documentation:

1. **Write scope.** Only companies whose name starts with a configured prefix
   (``TA-`` by default) may be written to. Any other company is refused before
   validation, before the approval queue, before anything is built. This is what
   protects a firm's real books if they later appear on the same machine.
2. **Educational-mode dates.** A student/EDU install only accepts vouchers dated
   on the 1st, 2nd or 31st. Enforced as a validation rule so the failure is a
   readable report line, not an opaque Tally error after the fact.

The guard is deliberately dumb and total: a prefix check on a string. Anything
cleverer (a list of allowed companies, a regex) would be a thing to get subtly
wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from tallyagent_core.errors import PolicyError

#: The shipped write-scope prefix. Companies the agent creates are named with
#: it, so the default install can do its whole demo without being able to touch
#: anything it did not make.
DEFAULT_WRITE_PREFIX = "TA-"


class WriteScopeError(PolicyError):
    """A write was aimed at a company outside the live-mode write scope."""


@dataclass(frozen=True, slots=True)
class LiveMode:
    """Whether we are live, and what we may touch when we are."""

    enabled: bool = False
    write_prefix: str = DEFAULT_WRITE_PREFIX
    edu: bool = False

    @property
    def name(self) -> str:
        return "live" if self.enabled else "fake"

    def may_write_to(self, company: str) -> bool:
        """In fake mode, anything. In live mode, only the prefixed scope."""
        if not self.enabled:
            return True
        if not self.write_prefix:
            # An empty prefix would mean "everything"; treat it as a
            # misconfiguration and allow nothing rather than allow all.
            return False
        return (company or "").startswith(self.write_prefix)

    def require_write_scope(self, company: str) -> None:
        """Raise unless ``company`` is writable. Call before building anything."""
        if self.may_write_to(company):
            return
        if not self.write_prefix:
            raise WriteScopeError(
                "live mode is on but [tally] write_prefix is empty, so no company "
                "is writable. Set write_prefix (default \"TA-\") in config.toml."
            )
        raise WriteScopeError(
            f"refusing to write to company {company!r}: live mode only writes to "
            f"companies whose name starts with {self.write_prefix!r}. This "
            "protects books tallyagent did not create. Rename the target "
            "company, or change [tally] write_prefix if you really mean it."
        )

    def describe(self) -> str:
        if not self.enabled:
            return "fake Tally (no real writes possible)"
        scope = f"writes limited to {self.write_prefix}*"
        return f"LIVE{' EDU' if self.edu else ''} - {scope}"
