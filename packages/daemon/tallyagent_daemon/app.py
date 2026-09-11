"""The FastAPI application: web UI, WhatsApp webhook, health, scheduler.

Binds to 127.0.0.1 by default. The daemon holds a live connection to the firm's
accounts; it is not a LAN service.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from tallyagent_channels.web.app import build_router as build_web_router
from tallyagent_channels.whatsapp.meta import MetaClient
from tallyagent_channels.whatsapp.webhook import build_router as build_whatsapp_router
from tallyagent_daemon.scheduler import Scheduler
from tallyagent_daemon.wiring import Wired

log = logging.getLogger(__name__)


def build_app(wired: Wired) -> FastAPI:
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
    app.include_router(build_web_router(wired.services))

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

    return app
