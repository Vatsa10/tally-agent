"""Every client's services, behind one daemon.

A CA works on many sets of books in one sitting and wants one browser tab for
all of them. Each client already has its own database - queue, audit chain,
consent - so the pool does not merge anything. It holds one :class:`Services`
per client slug and hands out the one a request names.

Clients are wired lazily and then kept: wiring opens a database and builds a
Tally client, which is cheap once and wasteful on every request, and a firm with
two hundred clients should not open two hundred files at startup. The factory
is supplied by the daemon so this package never learns how a client is wired.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tallyagent_channels.services import Services


@dataclass(frozen=True, slots=True)
class Entry:
    """One client as the picker shows it."""

    slug: str
    name: str


@dataclass(slots=True)
class ServicesPool:
    entries: list[Entry]
    factory: Callable[[str], Services]
    default: str = ""
    _wired: dict[str, Services] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValueError("a services pool needs at least one client")
        if not self.default:
            self.default = self.entries[0].slug
        if self.default not in self.slugs():
            raise ValueError(f"default client {self.default!r} is not in the pool")

    @classmethod
    def single(cls, services: Services, slug: str = "default") -> ServicesPool:
        """A single-company install: one entry, already wired."""
        pool = cls(
            entries=[Entry(slug, services.company.name)],
            factory=lambda _slug: services,
            default=slug,
        )
        pool._wired[slug] = services
        return pool

    def slugs(self) -> list[str]:
        return [entry.slug for entry in self.entries]

    def has(self, slug: str) -> bool:
        return slug in self.slugs()

    def get(self, slug: str) -> Services:
        """The client's services, wired on first use. An unknown slug raises
        KeyError rather than falling back, because falling back would show one
        client's queue under another client's name."""
        if not self.has(slug):
            raise KeyError(f"no client called {slug!r}")
        services = self._wired.get(slug)
        if services is None:
            services = self.factory(slug)
            self._wired[slug] = services
        return services

    def put(self, slug: str, services: Services) -> None:
        """Seed an already-wired client, so the daemon's default is not wired twice."""
        if not self.has(slug):
            raise KeyError(f"no client called {slug!r}")
        self._wired[slug] = services

    def pending_count(self, slug: str) -> int:
        services = self.get(slug)
        return len(services.queue.list("pending", services.company.name))
