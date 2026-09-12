"""One place where everything is constructed and joined up.

Having a single wiring function is what makes the security properties checkable:
there is exactly one executor, exactly one approval queue, and exactly one path
from a tool call to a Tally write.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlmodel import Session

from tallyagent_agent.memory import Memory
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import EgressRow, SqlIdempotencyStore, make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_approvals.voucher_index import VoucherIndex
from tallyagent_channels.services import Services
from tallyagent_daemon.config import Config
from tallyagent_llm.router import EgressRecord, Router, build_provider
from tallyagent_tally.backend import TallyBackend
from tallyagent_tally.client import TallyClient
from tallyagent_tools.base import ToolContext

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Wired:
    config: Config
    services: Services
    backend: TallyBackend
    engine: object

    @property
    def using_mock_model(self) -> bool:
        return self.services.router.name == "mock"


def build(
    config: Config,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Wired:
    """Construct the whole application from config.

    ``transport`` is how the demo and the tests attach the fake Tally without a
    socket; in production it is None and httpx opens a real connection.
    """
    engine = make_engine(config.db_path)
    audit = AuditLog(engine)

    client = TallyClient(config.tally, transport=transport)
    backend = TallyBackend(client, store=SqlIdempotencyStore(engine))

    provider = _provider_or_mock(config)
    router = Router(provider, on_egress=_egress_writer(engine, config))

    queue = ApprovalQueue(engine, audit)
    index = VoucherIndex(engine, config.company.name)
    tools = ToolContext(
        backend=backend,
        company=config.company,
        policy=config.policy,
        live=config.live,
        enqueue=queue.enqueue,
        ledger_aliases=queue.learned_aliases(config.company.name),
        voucher_index=index,
    )
    queue.executor = _executor(backend, tools, config)

    services = Services(
        company=config.company,
        tools=tools,
        queue=queue,
        audit=audit,
        router=router,
        memory=Memory(engine, config.company.name),
        max_steps=config.model.max_steps,
    )
    return Wired(config=config, services=services, backend=backend, engine=engine)


def _provider_or_mock(config: Config):  # type: ignore[no-untyped-def]
    """Fall back to the mock provider, loudly, when no key is configured.

    A CA firm trying the demo should get a working, obviously-fake agent rather
    than a stack trace - but they must be told, which is why this logs a warning
    and ``Wired.using_mock_model`` is checked by the CLI.
    """
    from tallyagent_core.errors import NotConfiguredError
    from tallyagent_llm.router import ModelConfig

    try:
        return build_provider(config.model)
    except NotConfiguredError as exc:
        log.warning("%s Falling back to the deterministic mock provider.", exc)
        return build_provider(ModelConfig(provider="mock"))


def _executor(backend: TallyBackend, tools: ToolContext, config: Config):  # type: ignore[no-untyped-def]
    """The single function that turns an approved action into a Tally write.

    It lives in the tools package so the daemon, the TUI, the web UI and every
    test share it rather than each keeping a near-copy that quietly misses a
    new action type.
    """
    from tallyagent_tools.executor import build_executor

    return build_executor(tools)


def _egress_writer(engine, config: Config):  # type: ignore[no-untyped-def]
    """Persist every model request to the database and to a JSONL file.

    Two sinks on purpose: the database is what the UI reads, the file is what a
    client's IT department can be handed when they ask what left the machine.
    """
    path = Path(config.logging.egress_log)

    def write(record: EgressRecord) -> None:
        with Session(engine) as session:
            session.add(
                EgressRow(
                    destination=record.destination,
                    provider=record.provider,
                    model=record.model,
                    bytes_sent=record.bytes_sent,
                    fields=",".join(record.fields),
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    cost_usd=format(record.cost_usd, "f"),
                )
            )
            session.commit()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "destination": record.destination,
                            "provider": record.provider,
                            "model": record.model,
                            "bytes_sent": record.bytes_sent,
                            "fields": record.fields,
                            "prompt_tokens": record.prompt_tokens,
                            "completion_tokens": record.completion_tokens,
                            "cost_usd": format(record.cost_usd, "f"),
                        }
                    )
                    + "\n"
                )
        except OSError as exc:  # noqa: BLE001 - a full disk must not stop the agent
            log.warning("could not append to the egress log: %s", exc)

    return write
