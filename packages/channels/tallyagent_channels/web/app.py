"""Local web UI: chat and approvals, for every client the practice works on.

HTMX plus a Tailwind CDN tag, served by the daemon on 127.0.0.1. No build step,
no Electron, no bundler - the whole UI is a few templates and a router, which is
what a tray application on a CA firm's Windows machine should cost.

Every page lives under ``/c/<slug>/``. The client is part of the address rather
than a cookie or a session, so a bookmarked approvals page always opens the same
client's books, and two tabs on two clients cannot interfere. The unprefixed
addresses from the single-company UI still work: they redirect to the default
client, which on a single-company install is the only one.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from tallyagent_channels.pool import ServicesPool
from tallyagent_channels.services import Services
from tallyagent_core.errors import TallyAgentError
from tallyagent_core.models import Voucher, VoucherLine

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_router(source: Services | ServicesPool, inbox: Any = None) -> APIRouter:
    """The web routes over one client or a pool of them.

    A bare :class:`Services` is a single-company install and is wrapped as a
    pool of one, so there is exactly one code path for both.
    """
    pool = source if isinstance(source, ServicesPool) else ServicesPool.single(source)
    router = APIRouter()

    def _services(slug: str) -> Services:
        try:
            return pool.get(slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no client {slug!r}") from exc

    def _ticket(services: Services, ticket: str):  # type: ignore[no-untyped-def]
        """The ticket, only if it belongs to this client's books.

        Each client's queue is its own database, so a ticket from another client
        is simply not found here. The company check is the second lock: a
        database shared between two register entries must still not let one
        client's voucher be approved under the other's address.
        """
        try:
            item = services.queue.get(ticket)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if item.company and item.company != services.company.name:
            raise HTTPException(
                status_code=404, detail=f"{ticket} does not belong to this client"
            )
        return item

    def _users(services: Services) -> list[object]:
        """Who may be picked in the decision form. Empty on a bare install, and
        the form then falls back to a plain name field. The register is
        firm-wide, so every client offers the same people."""
        people = getattr(services, "people", None)
        return [] if people is None else list(people.list())

    def _pending(services: Services) -> list[object]:
        return list(services.queue.list("pending", services.company.name))

    def render(
        request: Request, slug: str, template: str, **context: object
    ) -> HTMLResponse:
        services = _services(slug)
        context.setdefault("users", _users(services))
        clients = [
            {
                "slug": entry.slug,
                "name": entry.name,
                "pending": pool.pending_count(entry.slug),
            }
            for entry in pool.entries
        ]
        return TEMPLATES.TemplateResponse(
            request=request,
            name=template,
            context={
                "services": services,
                "slug": slug,
                "base": f"/c/{slug}",
                "clients": clients,
                **context,
            },
        )

    @router.get("/c/{slug}/", response_class=HTMLResponse)
    async def index(request: Request, slug: str) -> HTMLResponse:
        services = _services(slug)
        return render(
            request,
            slug,
            "index.html",
            status=services.status(),
            pending=_pending(services),
        )

    @router.get("/c/{slug}", include_in_schema=False)
    async def index_without_slash(slug: str) -> RedirectResponse:
        _services(slug)
        return RedirectResponse(f"/c/{slug}/", status_code=307)

    @router.get("/c/{slug}/approvals", response_class=HTMLResponse)
    async def approvals(
        request: Request, slug: str, status: str = "pending"
    ) -> HTMLResponse:
        services = _services(slug)
        return render(
            request,
            slug,
            "_approvals.html",
            pending=services.queue.list(status, services.company.name),
        )

    @router.get("/c/{slug}/approvals/{ticket}", response_class=HTMLResponse)
    async def approval_detail(request: Request, slug: str, ticket: str) -> HTMLResponse:
        item = _ticket(_services(slug), ticket)
        return render(request, slug, "_diff.html", item=item, diff=item.diff())

    def _decider(services: Services, action: str, name: str, pin: str) -> str:
        """Who is making this decision, or raise.

        There is no session to trust in an HTMX post, so the decision carries
        the proof: a registered name and that person's PIN, checked here. The
        old form took ``actor`` as a field the browser filled in, which meant
        every approval in the audit chain said "web" and any tab could post one.
        """
        people = getattr(services, "people", None)
        if people is None or people.empty:
            return name or "web"
        return people.authorise(action, name, pin).name

    @router.post("/c/{slug}/approvals/{ticket}/approve", response_class=HTMLResponse)
    async def approve(
        request: Request,
        slug: str,
        ticket: str,
        who: str = Form(""),
        pin: str = Form(""),
        reason: str = Form(""),
    ) -> HTMLResponse:
        services = _services(slug)
        _ticket(services, ticket)
        try:
            actor = _decider(services, "approve", who, pin)
        except TallyAgentError as exc:
            return render(
                request,
                slug,
                "_approvals.html",
                pending=_pending(services),
                flash=str(exc),
                flash_ok=False,
            )
        result = await services.queue.approve(ticket, actor, reason)
        message = (
            f"{ticket} posted to Tally"
            + (f" as voucher {result.voucher_number}" if result.voucher_number else "")
            if result.ok
            else f"{ticket} failed: {'; '.join(result.errors)}"
        )
        return render(
            request,
            slug,
            "_approvals.html",
            pending=_pending(services),
            flash=message,
            flash_ok=result.ok,
        )

    @router.post("/c/{slug}/approvals/{ticket}/reject", response_class=HTMLResponse)
    async def reject(
        request: Request,
        slug: str,
        ticket: str,
        reason: str = Form(...),
        who: str = Form(""),
        pin: str = Form(""),
    ) -> HTMLResponse:
        services = _services(slug)
        _ticket(services, ticket)
        try:
            services.queue.reject(ticket, _decider(services, "reject", who, pin), reason)
            flash, ok = f"{ticket} rejected.", True
        except (ValueError, TallyAgentError) as exc:
            flash, ok = str(exc), False
        return render(
            request,
            slug,
            "_approvals.html",
            pending=_pending(services),
            flash=flash,
            flash_ok=ok,
        )

    @router.post("/c/{slug}/approvals/{ticket}/edit", response_class=HTMLResponse)
    async def edit_and_approve(
        request: Request,
        slug: str,
        ticket: str,
        who: str = Form(""),
        pin: str = Form(""),
        reason: str = Form(""),
    ) -> HTMLResponse:
        """Replace ledger names and amounts line by line, then approve.

        The form posts ``ledger_0``/``amount_0`` pairs; anything missing keeps
        the original line, so a partial form cannot silently zero a voucher.
        """
        services = _services(slug)
        item = _ticket(services, ticket)
        try:
            actor = _decider(services, "edit_and_approve", who, pin)
        except TallyAgentError as exc:
            return render(
                request,
                slug,
                "_approvals.html",
                pending=_pending(services),
                flash=str(exc),
                flash_ok=False,
            )
        if item.voucher is None:
            return render(
                request,
                slug,
                "_approvals.html",
                pending=_pending(services),
                flash=f"{ticket} is not a voucher and cannot be edited here.",
                flash_ok=False,
            )

        form = await request.form()
        lines: list[VoucherLine] = []
        for index, original in enumerate(item.voucher.lines):
            ledger = str(form.get(f"ledger_{index}") or original.ledger_name)
            raw_amount = form.get(f"amount_{index}")
            amount = Decimal(str(raw_amount)) if raw_amount else original.amount
            lines.append(
                VoucherLine(
                    ledger_name=ledger,
                    amount=amount,
                    cost_centre=original.cost_centre,
                    bill_reference=original.bill_reference,
                )
            )
        edited: Voucher = item.voucher.model_copy(update={"lines": lines})

        if not edited.is_balanced:
            return render(
                request,
                slug,
                "_diff.html",
                item=item,
                diff=item.diff(),
                flash=(
                    f"Not applied: the edited voucher does not balance "
                    f"(Dr {edited.total_debit} vs Cr {edited.total_credit})."
                ),
                flash_ok=False,
            )

        result = await services.queue.edit_and_approve(ticket, actor, edited, reason)
        services.refresh_aliases()
        return render(
            request,
            slug,
            "_approvals.html",
            pending=_pending(services),
            flash=(
                f"{ticket} edited and posted."
                if result.ok
                else f"{ticket} failed: {'; '.join(result.errors)}"
            ),
            flash_ok=result.ok,
        )

    @router.post("/c/{slug}/chat", response_class=HTMLResponse)
    async def chat(
        request: Request,
        slug: str,
        message: str = Form(...),
        conversation: str = Form("web"),
    ) -> HTMLResponse:
        services = _services(slug)
        agent = services.agent(conversation)
        result = await agent.run(message)
        return render(
            request,
            slug,
            "_chat.html",
            question=message,
            answer=result.text,
            steps=result.steps,
            tickets=result.tickets,
            pending=_pending(services),
        )

    @router.get("/c/{slug}/health")
    async def client_health(slug: str) -> dict[str, object]:
        return _services(slug).status()

    def _inbox_page(
        request: Request, flash: str = "", flash_ok: bool = True
    ) -> HTMLResponse:
        services = _services(pool.default)
        items = inbox.open() if inbox is not None else []
        return TEMPLATES.TemplateResponse(
            request=request,
            name="_inbox.html",
            context={
                "items": items,
                "users": _users(services),
                "flash": flash,
                "flash_ok": flash_ok,
            },
        )

    @router.get("/firm/inbox", response_class=HTMLResponse)
    async def firm_inbox(request: Request) -> HTMLResponse:
        """Every client's open lines, worst first - the partner's morning."""
        return _inbox_page(request)

    @router.post("/firm/inbox/{item_id}/resolve", response_class=HTMLResponse)
    async def firm_inbox_resolve(
        request: Request, item_id: int, who: str = Form(""), pin: str = Form("")
    ) -> HTMLResponse:
        """Mark a line dealt with, by a named person.

        Any registered person may - dealing with a line is work, not a
        decision about the books - but it carries their name, checked by PIN,
        like everything else that says who did what.
        """
        if inbox is None:
            return _inbox_page(request, "No firm inbox here.", False)
        services = _services(pool.default)
        people = getattr(services, "people", None)
        name = who or "web"
        if people is not None and not people.empty:
            try:
                name = people.check(who, pin).name
            except TallyAgentError as exc:
                return _inbox_page(request, str(exc), False)
        done = inbox.resolve(item_id, name)
        return _inbox_page(
            request,
            f"Marked dealt with by {name}." if done else f"No line #{item_id}.",
            done,
        )

    @router.get("/health")
    async def health() -> dict[str, object]:
        """The default client's status, where the tray and monitors look."""
        status = pool.get(pool.default).status()
        status["clients"] = pool.slugs()
        return status

    @router.api_route("/", methods=["GET"], include_in_schema=False)
    async def root(request: Request) -> RedirectResponse:
        return _to_default(request, "")

    @router.api_route(
        "/{rest:path}", methods=["GET", "POST"], include_in_schema=False
    )
    async def unprefixed(request: Request, rest: str) -> RedirectResponse:
        """The single-company addresses, sent on to the default client.

        A 307 keeps the method and the form body, so an HTMX post from a page
        loaded before the upgrade still lands as the same decision.
        """
        if rest == "c" or rest.startswith("c/"):
            raise HTTPException(status_code=404)
        return _to_default(request, rest)

    def _to_default(request: Request, rest: str) -> RedirectResponse:
        target = f"/c/{pool.default}/{rest}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=307)

    return router
