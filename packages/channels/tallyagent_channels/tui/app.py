"""The terminal chat UI.

Left: the transcript. Right: Tally status above, the approval queue below.
Bottom: the input box. The point of the layout is that the two things a person
must not miss - what Tally is, and what is waiting for their approval - are
always on screen, never scrolled away by chat.

This module is deliberately thin. Everything it can do lives in ``session.py``
and is tested there; here there is only rendering and key handling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from tallyagent_channels.services import Services
from tallyagent_channels.tui import preview
from tallyagent_channels.tui.session import Session
from tallyagent_core.livemode import LiveMode

log = logging.getLogger(__name__)

KIND_STYLE = {
    "user": "bold",
    "agent": "",
    "tool": "dim",
    "system": "cyan",
    "error": "bold red",
    "approval": "yellow",
}

CSS = """
Screen { layout: vertical; }
#body { height: 1fr; }
#left { width: 65%; border-right: solid $panel; }
#right { width: 35%; }
#transcript { height: 1fr; padding: 0 1; }
#status { height: auto; max-height: 45%; border-bottom: solid $panel; padding: 0 1; }
#queue-box { height: 1fr; padding: 0 1; }
#queue { height: 1fr; }
#prompt { dock: bottom; height: 3; }
.line { margin-bottom: 1; }
.detail { color: $text-muted; }
ModalScreen { align: center middle; }
#modal { width: 80%; height: 80%; border: thick $warning; background: $surface; padding: 1; }
#modal-body { height: 1fr; }
"""


class Transcript(VerticalScroll):
    """The chat log. Tool calls are collapsed to one line with a detail beneath."""

    def add(self, kind: str, text: str, detail: str = "", meta: dict | None = None) -> None:
        style = KIND_STYLE.get(kind, "")
        prefix = {
            "user": "you  ",
            "agent": "",
            "tool": "  ->  ",
            "system": "",
            "error": "  !!  ",
            "approval": "  **  ",
        }.get(kind, "")

        label = f"[{style}]{prefix}{text}[/]" if style else f"{prefix}{text}"
        if kind == "agent" and meta:
            # Which model answered is never hidden: a mock answer and a real one
            # must never be mistaken for each other.
            label += f"\n[dim]      -- {meta.get('provider')}/{meta.get('model')}[/]"
        self.mount(Static(label, classes="line", markup=True))
        if detail:
            self.mount(Static(f"      {detail}", classes="line detail", markup=False))
        self.scroll_end(animate=False)


class StatusPanel(Static):
    """Tally status plus the Tier 2 ui_context, refreshed on a timer."""

    body: reactive[str] = reactive("checking Tally...")

    def render(self) -> str:  # type: ignore[override]
        return self.body


class ApprovalQueue(ListView):
    """Pending actions. Keys act on the highlighted one."""


class DetailModal(ModalScreen[bool]):
    """A scrollable blob: raw XML, a validation report, or a Tier 3 preview."""

    BINDINGS = [Binding("escape,q", "dismiss(False)", "close")]

    def __init__(self, title: str, body: str, approve_reject: bool = False) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._approve_reject = approve_reject

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label(self._title)
            with VerticalScroll(id="modal-body"):
                yield Static(self._body, markup=False)
            if self._approve_reject:
                with Horizontal():
                    yield Button("Approve (a)", variant="success", id="approve")
                    yield Button("Reject (r)", variant="error", id="reject")

    BINDINGS_EXTRA = [Binding("a", "approve", "approve"), Binding("r", "reject", "reject")]

    def on_mount(self) -> None:
        if self._approve_reject:
            for binding in self.BINDINGS_EXTRA:
                self._bindings.bind(binding.key, binding.action, binding.description)

    def action_approve(self) -> None:
        self.dismiss(True)

    def action_reject(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#approve")
    def _approved(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#reject")
    def _rejected(self) -> None:
        self.dismiss(False)


class ReasonModal(ModalScreen[str]):
    """Rejecting requires a reason, so asking for one is a modal, not a prompt."""

    BINDINGS = [Binding("escape", "dismiss('')", "cancel")]

    def __init__(self, ticket: str) -> None:
        super().__init__()
        self._ticket = ticket

    def compose(self) -> ComposeResult:
        with Vertical(id="modal"):
            yield Label(f"Why are you rejecting {self._ticket}?")
            yield Input(placeholder="reason (required)", id="reason")

    @on(Input.Submitted, "#reason")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class TallyAgentTUI(App[None]):
    """The app."""

    CSS = CSS
    TITLE = "tallyagent"
    BINDINGS = [
        Binding("ctrl+c,ctrl+q", "quit", "quit"),
        Binding("a", "approve", "approve", show=True),
        Binding("shift+a", "approve_all", "approve all"),
        Binding("r", "reject", "reject"),
        Binding("e", "edit", "edit"),
        Binding("x", "raw_xml", "raw XML"),
        Binding("v", "validation", "validation"),
        Binding("f5", "refresh_status", "refresh"),
    ]

    def __init__(
        self,
        services: Services,
        live: LiveMode | None = None,
        status_interval: float = 5.0,
    ) -> None:
        super().__init__()
        self.services = services
        self.live = live or LiveMode()
        self.session = Session(services, live=self.live)
        self.session.tier3_approver = self._approve_tier3_step
        self.status_interval = status_interval

    # --- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Transcript(id="transcript")
            with Vertical(id="right"):
                yield StatusPanel(id="status")
                with Vertical(id="queue-box"):
                    yield Label("Awaiting approval")
                    yield ApprovalQueue(id="queue")
        yield Input(placeholder="ask, or /help", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        transcript = self.query_one(Transcript)
        transcript.add(
            "system",
            f"tallyagent - {self.services.company.name or '(no company yet)'}  "
            f"[{self.live.describe()}]",
        )
        transcript.add(
            "system",
            f"model: {self.services.router.name}/{self.services.router.model}"
            + (
                "   (mock: no API key configured, answers are deterministic)"
                if self.services.router.name == "mock"
                else ""
            ),
        )
        transcript.add("system", "Type /help for commands. Nothing posts without you.")
        self.set_interval(self.status_interval, self.refresh_status)
        self.refresh_status()
        self.refresh_queue()

    # --- panels -------------------------------------------------------------

    @work(exclusive=True, group="status")
    async def refresh_status(self) -> None:
        panel = self.query_one(StatusPanel)
        try:
            from tallyagent_tally.probe import probe as run_probe

            report = await run_probe(self.services.tools.backend.client, self.live)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - the panel must never kill the app
            panel.body = f"[red]status unavailable: {exc}[/]"
            return

        edu = " [bold yellow]EDU[/]" if report.edition == "educational" else ""
        lines = [
            f"[bold]Tally[/]{edu}  {'up' if report.reachable else '[red]down[/]'}",
            f"  {report.url}",
            f"  version   {report.version or '?'}",
            f"  loaded    {', '.join(report.companies_loaded) or '(none)'}",
            f"  scope     {self.live.describe()}",
        ]
        ui_context = getattr(self.services, "ui_context", None)
        if ui_context is not None and getattr(ui_context, "screen", ""):
            lines += [
                "",
                "[bold]On screen[/] (Tier 2)",
                f"  {ui_context.screen}",
                f"  {ui_context.period}",
            ]
        lines += [
            "",
            f"[bold]Model[/] {self.services.router.name}/{self.services.router.model}",
            f"  tokens    {self.services.router.total_tokens}",
            f"  cost      ${self.services.router.total_cost}",
        ]
        panel.body = "\n".join(lines)

    def refresh_queue(self) -> None:
        queue = self.query_one(ApprovalQueue)
        queue.clear()
        for item in self.session.pending():
            flag = (
                ""
                if item.validation is None or item.validation.ok
                else "  [red]VALIDATION FAILED[/]"
            )
            queue.append(
                ListItem(
                    Static(
                        f"[bold]{item.ticket}[/]  {format(item.amount, 'f')}{flag}\n"
                        f"[dim]{item.summary}[/]",
                        markup=True,
                    ),
                    name=item.ticket,
                )
            )

    def _selected_ticket(self) -> str | None:
        queue = self.query_one(ApprovalQueue)
        item = queue.highlighted_child
        return item.name if item is not None else None

    # --- input --------------------------------------------------------------

    @on(Input.Submitted, "#prompt")
    async def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self.run_turn(text)

    @work(group="turn")
    async def run_turn(self, text: str) -> None:
        transcript = self.query_one(Transcript)
        turn = await self.session.handle(text)
        for line in turn.lines:
            transcript.add(line.kind, line.text, line.detail, line.meta)
        self.refresh_queue()
        self.refresh_status()
        if turn.quit:
            self.exit()

    # --- approval keys ------------------------------------------------------

    @work(group="approve")
    async def action_approve(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            return
        await self._run_command(f"/approve {ticket}")

    @work(group="approve")
    async def action_approve_all(self) -> None:
        await self._run_command("/approve A")

    @work(group="approve")
    async def action_reject(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            return
        reason = await self.push_screen_wait(ReasonModal(ticket))
        if not reason:
            self.query_one(Transcript).add(
                "system", "Rejection cancelled - a reason is required."
            )
            return
        await self._run_command(f"/reject {ticket} {reason}")

    async def _run_command(self, command: str) -> None:
        transcript = self.query_one(Transcript)
        turn = await self.session.handle(command)
        for line in turn.lines:
            transcript.add(line.kind, line.text, line.detail, line.meta)
        self.refresh_queue()

    @work(group="modal")
    async def action_raw_xml(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            return
        item = self.services.queue.get(ticket)
        await self.push_screen_wait(
            DetailModal(f"{ticket} - XML that will be sent to Tally", item.raw_xml)
        )

    @work(group="modal")
    async def action_validation(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            return
        item = self.services.queue.get(ticket)
        diff = item.diff()
        body = [f"{item.summary}", ""]
        body += [
            f"{row['ledger']:<28}{row['debit']:>14}{row['credit']:>14}"
            for row in diff["ledger_impact"]
        ]
        body += [
            f"{'':<28}{'-' * 14}{'-' * 14}",
            f"{'total':<28}{diff['totals']['debit']:>14}{diff['totals']['credit']:>14}",
            "",
            "Validation:",
        ]
        body += [
            f"  {'PASS' if r['passed'] else r['severity'].upper():<8}{r['rule']}: "
            f"{r['message']}"
            for r in diff["validation"]["results"]
        ]
        await self.push_screen_wait(DetailModal(f"{ticket} - diff", "\n".join(body)))

    @work(group="modal")
    async def action_edit(self) -> None:
        ticket = self._selected_ticket()
        if ticket is None:
            return
        # Editing line by line in a terminal form is more UI than it is worth;
        # the chat can do it in one sentence and goes through the same queue.
        self.query_one(Transcript).add(
            "system",
            f"To change {ticket}, say what to change in the chat - for example "
            f'"change {ticket} to use Bank - HDFC 1234 instead of Cash". '
            "It re-queues with the correction and learns the alias.",
        )

    def action_refresh_status(self) -> None:
        self.refresh_status()
        self.refresh_queue()

    # --- Tier 3 -------------------------------------------------------------

    async def _approve_tier3_step(self, step: Any) -> bool:
        """Show the screenshot and the proposed keystroke; require a decision.

        This is the hook that closes the "Tier 3 has no UI" gap: without it a
        mutating step has no approver and the fallback stops rather than acting.
        """
        self.session.stats.tier3_used = True
        body = preview.describe_step(step, width=70)
        return bool(
            await self.push_screen_wait(
                DetailModal("Tier 3 - approve this step?", body, approve_reject=True)
            )
        )


def run(services: Services, live: LiveMode | None = None) -> None:
    TallyAgentTUI(services, live).run()


async def run_async(services: Services, live: LiveMode | None = None) -> None:
    await TallyAgentTUI(services, live).run_async()


__all__ = ["TallyAgentTUI", "run", "run_async", "asyncio"]
