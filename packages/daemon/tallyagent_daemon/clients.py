"""The client register: one practice, many sets of books.

A firm is not one company. It is thirty to two hundred, some in a TallyPrime on
the desk in the corner and some on the client's own machine across a VPN, and
the work is the same work done once per client. Everything up to here assumed
one: one config, one database, one company.

Three decisions shape this file:

- **A client is a connection plus a company.** Host, port, company name, state
  code, and the paths that differ per client (where their scans land, which bank
  ledger their statement belongs to). Nothing secret: the Tally password and the
  model key still come from the environment, the same for every client.
- **One database per client.** Not one database with a client column. The
  approval queue, the audit chain, the idempotency keys and the learned ledger
  aliases are all per-client, and the strongest guarantee available is that one
  client's ticket cannot be approved against another client's books because it
  is not in the same file. It also means a client can be handed over or deleted
  by moving one file.
- **The file is a register, not a state machine.** Which client is *active* is a
  property of a session, not of the install, so two windows can work on two
  clients at once.

The consent table lives in each client's own database, so enabling a company is
also per-client and cannot leak sideways.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from tallyagent_core.errors import TallyAgentError

#: Slugs name a database file and appear in every command, so they are kept to
#: what is safe in a filename and readable in a prompt.
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

DEFAULT_REGISTER = "config/clients.toml"
DEFAULT_DATA_DIR = "data"


class UnknownClientError(TallyAgentError):
    """A client slug that is not in the register."""


@dataclass(frozen=True, slots=True)
class Client:
    """One set of books this practice works on."""

    slug: str
    name: str
    company: str
    host: str = "127.0.0.1"
    port: int = 9000
    state_code: str = "27"
    gstin: str = ""
    #: Where this client's scanned purchase bills arrive.
    bills_dir: str = ""
    #: Which bank ledger their statement belongs to, for the close pack.
    bank_ledger: str = ""
    #: Overrides the derived path only when somebody has a reason to.
    db_path: str = ""
    notes: str = ""

    @property
    def is_remote(self) -> bool:
        return self.host not in ("127.0.0.1", "localhost", "::1")

    def database(self, data_dir: str | Path = DEFAULT_DATA_DIR) -> str:
        """The client's own database file."""
        return self.db_path or str(Path(data_dir) / f"{self.slug}.db")

    def describe(self) -> str:
        where = f"{self.host}:{self.port}" if self.is_remote else "this machine"
        return f"{self.slug:<16} {self.name:<28} {self.company:<24} {where}"


@dataclass(slots=True)
class Register:
    """Every client, in the order the file lists them."""

    clients: list[Client] = field(default_factory=list)
    data_dir: str = DEFAULT_DATA_DIR
    path: str = DEFAULT_REGISTER

    @property
    def empty(self) -> bool:
        return not self.clients

    def get(self, slug: str) -> Client:
        wanted = (slug or "").strip().lower()
        for client in self.clients:
            if client.slug == wanted:
                return client
        known = ", ".join(c.slug for c in self.clients) or "none"
        raise UnknownClientError(
            f"no client called {slug!r} in {self.path}. Registered: {known}."
        )

    def find(self, slug: str) -> Client | None:
        try:
            return self.get(slug)
        except UnknownClientError:
            return None

    def slugs(self) -> list[str]:
        return [client.slug for client in self.clients]


def parse(data: dict[str, Any], path: str = DEFAULT_REGISTER) -> Register:
    """Read a register out of already-loaded TOML.

    Split from :func:`load` so the shape can be tested without a file, and so a
    caller holding config from somewhere else can use it.
    """
    raw_clients = data.get("client") or data.get("clients") or []
    if isinstance(raw_clients, dict):
        # ``[client.sharma]`` style: the table key is the slug.
        items = [{"slug": slug, **(body or {})} for slug, body in raw_clients.items()]
    else:
        items = list(raw_clients)

    clients: list[Client] = []
    seen: set[str] = set()
    for item in items:
        slug = str(item.get("slug") or "").strip().lower()
        if not SLUG.match(slug):
            raise ValueError(
                f"{slug!r} is not a usable client id. Lower case letters, digits "
                "and hyphens, 2 to 40 characters - it names a database file and "
                "gets typed at a prompt."
            )
        if slug in seen:
            raise ValueError(f"client {slug!r} is listed twice in {path}")
        seen.add(slug)
        company = str(item.get("company") or "").strip()
        if not company:
            raise ValueError(
                f"client {slug!r} has no company name. That is the name Tally "
                "knows the books by, and every read and write needs it."
            )
        clients.append(
            Client(
                slug=slug,
                name=str(item.get("name") or company),
                company=company,
                host=str(item.get("host") or "127.0.0.1"),
                port=int(item.get("port") or 9000),
                state_code=str(item.get("state_code") or "27"),
                gstin=str(item.get("gstin") or ""),
                bills_dir=str(item.get("bills_dir") or ""),
                bank_ledger=str(item.get("bank_ledger") or ""),
                db_path=str(item.get("db_path") or ""),
                notes=str(item.get("notes") or ""),
            )
        )

    return Register(
        clients=clients,
        data_dir=str((data.get("storage") or {}).get("data_dir") or DEFAULT_DATA_DIR),
        path=path,
    )


def load(path: str | Path = DEFAULT_REGISTER) -> Register:
    """Read the register. A missing file is an empty register, not an error -
    a single-client install never makes one."""
    file = Path(path)
    if not file.exists():
        return Register(path=str(path))
    with file.open("rb") as handle:
        return parse(tomllib.load(handle), path=str(path))


def apply(config: Any, client: Client, data_dir: str = "") -> Any:
    """A copy of the config pointed at one client.

    The Tally connection, the company and the database all move together. They
    have to: a config with this client's company and the last client's database
    would put one client's vouchers in another's approval queue, and the company
    name is what the queue is filtered by.
    """
    from tallyagent_core.models import Company

    company = Company(
        name=client.company,
        state_code=client.state_code,
        gstin=client.gstin or None,
        period=config.company.period,
    )
    return replace(
        config,
        tally=replace(
            config.tally, host=client.host, port=client.port, company=client.company
        ),
        company=company,
        db_path=client.database(data_dir or DEFAULT_DATA_DIR),
        # firm_db_path is deliberately untouched: the people who work here are
        # registered once for the practice, not once per client.
    )
