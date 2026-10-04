"""The FastAPI application: web UI, WhatsApp webhook, health, scheduler.

Binds to 127.0.0.1 by default. The daemon holds a live connection to the firm's
accounts; it is not a LAN service.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from tallyagent_channels.pool import ServicesPool
from tallyagent_channels.web.app import build_router as build_web_router
from tallyagent_channels.whatsapp.meta import MetaClient
from tallyagent_channels.whatsapp.webhook import build_router as build_whatsapp_router
from tallyagent_daemon.scheduler import Scheduler
from tallyagent_daemon.wiring import Wired

log = logging.getLogger(__name__)


def build_app(wired: Wired, pool: ServicesPool | None = None) -> FastAPI:
    """The daemon's application.

    ``pool`` holds every client the web UI may show; without one the UI serves
    ``wired`` alone, as a single-company install always has. The WhatsApp
    webhook and the scheduler stay on ``wired``: a phone number and a nightly
    job belong to one set of books, not to whichever tab is open.
    """
    scheduler = Scheduler(wired)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(scheduler.run_forever())
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="tallyagent",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.wired = wired
    if wired.config.whatsapp.enabled:
        if not wired.config.whatsapp.app_secret:
            # Refuse rather than serve an endpoint that cannot authenticate.
            raise RuntimeError(
                "WhatsApp is enabled but WHATSAPP_APP_SECRET is not set. Set it, "
                "or disable [channels.whatsapp] in config.toml."
            )
        app.include_router(
            build_whatsapp_router(
                wired.services,
                wired.config.whatsapp,
                MetaClient(wired.config.whatsapp),
            )
        )
        log.info("WhatsApp webhook mounted at /whatsapp/webhook")
    else:
        log.info("WhatsApp channel is disabled")

    # Last, because the web router redirects every unprefixed address to the
    # default client and would otherwise swallow the webhook.
    app.include_router(build_web_router(pool or wired.services))

    return app
