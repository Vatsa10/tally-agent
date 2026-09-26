"""Live-mode guard rails.

Everything through Stage 13 ran against a fake Tally, where a mistake costs
nothing. Against a real installation a mistake writes to somebody's books, so
live mode adds two constraints that hold *in code*, not in documentation:

1. **Write scope.** A company is writable only if a partner has enabled it, or
   if it carries the prefix this install creates its own companies with (``TA-``
   by default). Anything else is refused before validation, before the approval
   queue, before anything is built. This is what protects a firm's real books
   when they appear on the same machine - and the prefix alone was not enough,
   because no real company is called ``TA-Sharma Textiles``: it left a firm
   choosing between renaming a client's books and turning the guard off.

   The list of enabled companies lives in ``tallyagent_approvals.consent`` and
   is injected here, so this module stays a pure guard with no database in it.
2. **Educational-mode dates.** A student/EDU install only accepts vouchers dated
   on the 1st, 2nd or 31st. Enforced as a validation rule so the failure is a
   readable report line, not an opaque Tally error after the fact.

The guard is deliberately dumb and total: a prefix check on a string, plus a
lookup that answers yes or no. No pattern matching, no inference from what a
company looks like.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from tallyagent_core.errors import PolicyError

#: The shipped write-scope prefix. Companies the agent creates are named with
#: it, so the default install can do its whole demo without being able to touch
#: anything it did not make.
DEFAULT_WRITE_PREFIX = "TA-"


class WriteScopeError(PolicyError):
    """A write was aimed at a company outside the live-mode write scope."""


#: Answers "may we write to this company, as it is loaded right now?" - with an
#: empty string for yes and the reason for no. A reason rather than a bool
#: because the two ways of being refused need different sentences: nobody has
#: enabled these books, and these are not the books that were enabled.
#: ``tallyagent_approvals.consent.ConsentStore.refusal`` satisfies it. Typed as
#: a callable so core keeps no dependency on the database layer.
ConsentLookup = Callable[[str, str], str]


@dataclass(frozen=True, slots=True)
class LiveMode:
    """Whether we are live, and what we may touch when we are."""

    enabled: bool = False
    write_prefix: str = DEFAULT_WRITE_PREFIX
    edu: bool = False
    #: Set at wiring time. Without it only the prefix scope is writable, which
    #: is the right behaviour for a fresh install: it can demo itself and touch
    #: nothing else.
    consents: ConsentLookup | None = None
    #: What Tally currently reports as the open company's GUID, when it is
    #: known. Consent is granted to the books, so the check compares it.
    company_guid: str = ""

    @property
    def name(self) -> str:
        return "live" if self.enabled else "fake"

    def may_write_to(self, company: str) -> bool:
        """In fake mode, anything. In live mode, the prefix scope or a consent."""
        if not self.enabled:
            return True
        if self.write_prefix and (company or "").startswith(self.write_prefix):
            return True
        if self.consents is None:
            return False
        return not self.consents(company or "", self.company_guid)

    def for_company(self, company_guid: str) -> LiveMode:
        """The same guard, told what Tally has open. Cheap and copy-on-write, so
        a caller can hand the guard a freshly read GUID without mutating the
        one the rest of the process shares."""
        return replace(self, company_guid=company_guid or "")

    def require_write_scope(self, company: str) -> None:
        """Raise unless ``company`` is writable. Call before building anything."""
        if self.may_write_to(company):
            return
        if self.consents is not None:
            raise WriteScopeError(self.consents(company or "", self.company_guid))
        if not self.write_prefix:
            raise WriteScopeError(
                "live mode is on but [tally] write_prefix is empty, so no company "
                "is writable. Set write_prefix (default \"TA-\") in config.toml."
            )
        raise WriteScopeError(
            f"refusing to write to company {company!r}: nobody has enabled it. "
            "Live mode writes to companies a partner has explicitly enabled, "
            f'with `tallyagent consent add "{company}"`, and to companies this '
            f"install created itself ({self.write_prefix}...). The grant is "
            "recorded with the partner's name, so the books can always say who "
            "allowed this."
        )

    def describe(self) -> str:
        if not self.enabled:
            return "fake Tally (no real writes possible)"
        scope = (
            "writes limited to enabled companies"
            if self.consents is not None
            else f"writes limited to {self.write_prefix}*"
        )
        return f"LIVE{' EDU' if self.edu else ''} - {scope}"
