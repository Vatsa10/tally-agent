"""Building a firm run from the files a practice actually has.

The CLI, the TUI, the daemon's scheduler and the web UI all need the same
thing: the register, a way to wire each client, the firm inbox, the jobs and the
autonomy policy. One function builds it, so the morning run is the same run
whichever surface started it.
"""

from __future__ import annotations

from typing import Any

from tallyagent_approvals.db import make_engine
from tallyagent_daemon import clients as clients_mod
from tallyagent_daemon import wiring
from tallyagent_daemon.firm.autonomy import EarnedAutonomy
from tallyagent_daemon.firm.inbox import Inbox
from tallyagent_daemon.firm.runner import FirmRunner


def make_runner(base: Any, register: Any, fake: bool = False) -> FirmRunner:
    """A runner over every registered client.

    Each client is wired once per run and cached, so the jobs and the autonomy
    pass share one connection and one database handle per client. ``fake``
    gives every client its own in-process fake Tally - for the demo and for a
    firm trying the product before pointing it at real books.
    """
    cache: dict[str, Any] = {}

    def wire(client: Any) -> Any:
        if client.slug in cache:
            return cache[client.slug]
        config = clients_mod.apply(base, client, register.data_dir)
        transport = None
        if fake:
            from tallyagent_tally.fake_server import seeded_demo

            transport = seeded_demo(client.company).transport
        wired = wiring.build(config, transport=transport)
        cache[client.slug] = wired
        return wired

    return FirmRunner(
        register,
        wire,
        firm_inbox(base),
        autonomy=EarnedAutonomy(),
    )


def firm_inbox(base: Any) -> Inbox:
    return Inbox(make_engine(base.firm_db_path))


def single_client_register(base: Any) -> Any:
    """A register of one, for an install that never wrote a clients.toml.

    The firm loop should work for a solo CA with one client too; making them
    write a register to get a morning brief would be ceremony.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in base.company.name.lower()).strip("-")
    slug = (slug or "books")[:38].strip("-") or "books"
    if len(slug) < 2:
        slug = f"{slug}-1"
    return clients_mod.Register(
        clients=[
            clients_mod.Client(
                slug=slug,
                name=base.company.name,
                company=base.company.name,
                host=base.tally.host,
                port=base.tally.port,
                state_code=base.company.state_code,
                gstin=base.company.gstin or "",
                db_path=base.db_path,
            )
        ]
    )
