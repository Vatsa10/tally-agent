"""Local web UI: chat and approvals.

HTMX plus a Tailwind CDN tag, served by the daemon on 127.0.0.1. No build step,
no Electron, no bundler - the whole UI is three templates and a router, which is
what a tray application on a CA firm's Windows machine should cost.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from tallyagent_channels.services import Services
from tallyagent_core.models import Voucher, VoucherLine

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_router(services: Services) -> APIRouter:
    router = APIRouter()

    def render(request: Request, template: str, **context: object) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request=request, name=template, context={"services": services, **context}
        )

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return render(
            request,
            "index.html",
            status=services.status(),
            pending=services.queue.list("pending", services.company.name),
        )

    @router.get("/approvals", response_class=HTMLResponse)
    async def approvals(request: Request, status: str = "pending") -> HTMLResponse:
        return render(
            request,
            "_approvals.html",
            pending=services.queue.list(status, services.company.name),
        )

    @router.get("/approvals/{ticket}", response_class=HTMLResponse)
    async def approval_detail(request: Request, ticket: str) -> HTMLResponse:
        item = services.queue.get(ticket)
        return render(request, "_diff.html", item=item, diff=item.diff())

    @router.post("/approvals/{ticket}/approve", response_class=HTMLResponse)
    async def approve(
        request: Request,
        ticket: str,
        actor: str = Form("web"),
        reason: str = Form(""),
    ) -> HTMLResponse:
        result = await services.queue.approve(ticket, actor, reason)
        message = (
            f"{ticket} posted to Tally"
            + (f" as voucher {result.voucher_number}" if result.voucher_number else "")
            if result.ok
            else f"{ticket} failed: {'; '.join(result.errors)}"
        )
        return render(
            request,
            "_approvals.html",
            pending=services.queue.list("pending", services.company.name),
            flash=message,
            flash_ok=result.ok,
        )

    @router.post("/approvals/{ticket}/reject", response_class=HTMLResponse)
    async def reject(
        request: Request,
        ticket: str,
        reason: str = Form(...),
        actor: str = Form("web"),
    ) -> HTMLResponse:
        try:
            services.queue.reject(ticket, actor, reason)
            flash, ok = f"{ticket} rejected.", True
        except ValueError as exc:
            flash, ok = str(exc), False
        return render(
            request,
            "_approvals.html",
            pending=services.queue.list("pending", services.company.name),
            flash=flash,
            flash_ok=ok,
        )

    @router.post("/approvals/{ticket}/edit", response_class=HTMLResponse)
    async def edit_and_approve(
        request: Request,
        ticket: str,
        actor: str = Form("web"),
        reason: str = Form(""),
    ) -> HTMLResponse:
        """Replace ledger names and amounts line by line, then approve.

        The form posts ``ledger_0``/``amount_0`` pairs; anything missing keeps
        the original line, so a partial form cannot silently zero a voucher.
        """
        item = services.queue.get(ticket)
        if item.voucher is None:
            return render(
                request,
                "_approvals.html",
                pending=services.queue.list("pending", services.company.name),
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
            "_approvals.html",
            pending=services.queue.list("pending", services.company.name),
            flash=(
                f"{ticket} edited and posted."
                if result.ok
                else f"{ticket} failed: {'; '.join(result.errors)}"
            ),
            flash_ok=result.ok,
        )

    @router.post("/chat", response_class=HTMLResponse)
    async def chat(
        request: Request, message: str = Form(...), conversation: str = Form("web")
    ) -> HTMLResponse:
        agent = services.agent(conversation)
        result = await agent.run(message)
        return render(
            request,
            "_chat.html",
            question=message,
            answer=result.text,
            steps=result.steps,
            tickets=result.tickets,
            pending=services.queue.list("pending", services.company.name),
        )

    @router.get("/health")
    async def health() -> dict[str, object]:
        return services.status()

    return router
