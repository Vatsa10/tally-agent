"""Who works here, and which of them may decide anything.

Two roles and no more. A **clerk** does the work - drafts vouchers, reads
reports, runs the bill batch, the reconciliations and the month-end close. A
**partner** decides: approves or rejects a ticket, edits and approves one,
grants a company write consent, changes policy, and turns the keyboard tier on.

The PIN is deliberately modest. It is not trying to keep an attacker off a
machine they are sitting in front of; it is there so an approval carries the
name of somebody who will stand behind it, rather than whatever the browser put
in a form field. Verified with scrypt and a per-user salt, so the table is not a
list of PINs even though the PINs are four digits.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlmodel import Session, select

from tallyagent_approvals.db import UserRow
from tallyagent_core.errors import PolicyError

log = logging.getLogger(__name__)

CLERK = "clerk"
PARTNER = "partner"
ROLES = (CLERK, PARTNER)

#: What only a partner may do. Everything else a clerk may do, because a firm
#: where the junior cannot draft is a firm that goes back to typing in Tally.
PARTNER_ONLY = (
    "approve",
    "reject",
    "edit_and_approve",
    "grant_consent",
    "revoke_consent",
    "change_policy",
    "enable_keyboard_tier",
)

#: Short enough that people will use it, long enough not to be a coin flip.
PIN_PATTERN = re.compile(r"^\d{4,8}$")

#: scrypt parameters. Interactive-login cost: a wrong PIN should be slow enough
#: that guessing ten thousand of them is not a lunch break.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class NotSignedInError(PolicyError):
    """Something was attempted with nobody signed in."""


class WrongPinError(PolicyError):
    """The name is known and the PIN is not."""


class NotPermittedError(PolicyError):
    """A real person, signed in, who is not allowed to do this."""


@dataclass(frozen=True, slots=True)
class User:
    name: str
    role: str = CLERK

    @property
    def is_partner(self) -> bool:
        return self.role == PARTNER

    def may(self, action: str) -> bool:
        return self.is_partner or action not in PARTNER_ONLY

    def require(self, action: str) -> None:
        if self.may(action):
            return
        raise NotPermittedError(
            f"{self.name} is a {self.role}, and {action.replace('_', ' ')} is a "
            "partner's decision. Ask a partner to sign in."
        )


def hash_pin(pin: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hash a PIN for storage. Returns ``(hash_hex, salt_hex)``."""
    if not PIN_PATTERN.match(pin or ""):
        raise ValueError("a PIN is 4 to 8 digits")
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        pin.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return digest.hex(), salt.hex()


class People:
    """The user table, and the one signed-in person this process is acting as."""

    def __init__(self, engine: Engine, audit: object | None = None) -> None:
        self.engine = engine
        self.audit = audit
        self.current: User | None = None

    # --- the register --------------------------------------------------------

    def add(self, name: str, role: str, pin: str, by: str = "setup") -> User:
        """Register a person. The first one has to be a partner, or nothing can
        ever be approved and the install is a read-only toy."""
        name = name.strip()
        if not name:
            raise ValueError("a user needs a name")
        if role not in ROLES:
            raise ValueError(f"role is one of {', '.join(ROLES)}, not {role!r}")
        digest, salt = hash_pin(pin)
        with Session(self.engine) as session:
            existing = session.exec(select(UserRow).where(UserRow.name == name)).first()
            if existing is not None:
                raise ValueError(f"{name!r} is already registered")
            session.add(
                UserRow(name=name, role=role, pin_hash=digest, pin_salt=salt)
            )
            session.commit()
        self._note("user_added", by, name=name, role=role)
        return User(name=name, role=role)

    def set_pin(self, name: str, pin: str, by: str = "setup") -> None:
        digest, salt = hash_pin(pin)
        with Session(self.engine) as session:
            row = self._row(session, name)
            row.pin_hash, row.pin_salt = digest, salt
            session.add(row)
            session.commit()
        self._note("user_pin_changed", by, name=name)

    def set_role(self, name: str, role: str, by: str) -> User:
        if role not in ROLES:
            raise ValueError(f"role is one of {', '.join(ROLES)}, not {role!r}")
        with Session(self.engine) as session:
            row = self._row(session, name)
            row.role = role
            session.add(row)
            session.commit()
        self._note("user_role_changed", by, name=name, role=role)
        return User(name=name, role=role)

    def disable(self, name: str, by: str) -> None:
        with Session(self.engine) as session:
            row = self._row(session, name)
            row.disabled = True
            session.add(row)
            session.commit()
        self._note("user_disabled", by, name=name)

    def list(self) -> list[User]:
        with Session(self.engine) as session:
            rows = session.exec(select(UserRow).order_by(UserRow.name)).all()
        return [User(name=r.name, role=r.role) for r in rows if not r.disabled]

    @property
    def empty(self) -> bool:
        """A fresh install, where the first thing to do is register a partner."""
        return not self.list()

    def get(self, name: str) -> User | None:
        with Session(self.engine) as session:
            row = session.exec(select(UserRow).where(UserRow.name == name)).first()
        if row is None or row.disabled:
            return None
        return User(name=row.name, role=row.role)

    # --- signing in ----------------------------------------------------------

    def check(self, name: str, pin: str) -> User:
        """Verify a name and PIN. Raises rather than returning a falsy user, so
        a caller cannot forget to look at the answer."""
        with Session(self.engine) as session:
            row = session.exec(
                select(UserRow).where(UserRow.name == (name or "").strip())
            ).first()
        if row is None or row.disabled:
            # Deliberately the same message as a wrong PIN: which names are
            # registered is not something a form should tell whoever asks.
            self._note("signin_failed", name or "?", reason="unknown or wrong pin")
            raise WrongPinError("that name and PIN do not match a user here")
        expected, _ = hash_pin(pin, bytes.fromhex(row.pin_salt))
        if not hmac.compare_digest(expected, row.pin_hash):
            self._note("signin_failed", row.name, reason="wrong pin")
            raise WrongPinError("that name and PIN do not match a user here")
        return User(name=row.name, role=row.role)

    def sign_in(self, name: str, pin: str) -> User:
        user = self.check(name, pin)
        self.current = user
        self._note("signed_in", user.name, role=user.role)
        return user

    def sign_out(self) -> None:
        if self.current is not None:
            self._note("signed_out", self.current.name)
        self.current = None

    def require_signed_in(self) -> User:
        if self.current is None:
            raise NotSignedInError(
                "nobody is signed in. /signin <name> first - a ticket has to "
                "carry the name of whoever decided it."
            )
        return self.current

    def require(self, action: str) -> User:
        """The signed-in person, if they may do this. Raises if not."""
        user = self.require_signed_in()
        user.require(action)
        return user

    def authorise(self, action: str, name: str, pin: str) -> User:
        """Name plus PIN, checked at the moment of the decision.

        This is what the web form and the WhatsApp reply use: there is no
        session to trust, so the decision itself carries the proof.
        """
        user = self.check(name, pin)
        user.require(action)
        return user

    # --- helpers -------------------------------------------------------------

    def _row(self, session: Session, name: str) -> UserRow:
        row = session.exec(select(UserRow).where(UserRow.name == name)).first()
        if row is None:
            raise ValueError(f"no user called {name!r}")
        return row

    def _note(self, event: str, actor: str, **payload: object) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(event, actor=actor, **payload)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the audit chain must not block a login
            log.exception("could not record %s", event)


def utc_now() -> datetime:
    return datetime.now(UTC)
