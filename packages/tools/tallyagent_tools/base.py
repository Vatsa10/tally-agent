"""Shared plumbing for the typed agent tools.

Every tool is an ordinary async function with typed arguments. They are the
only thing the agent, the MCP server and the channels are allowed to call - no
component reaches past them to the backend, because this is the layer that
enforces "validate, then queue, then maybe write".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tallyagent_backends.accounting_backend import AccountingBackend, WriteResult
from tallyagent_core import idempotency
from tallyagent_core.livemode import LiveMode
from tallyagent_core.models import Company, Voucher
from tallyagent_core.policy import Policy
from tallyagent_core.validation import (
    PostedVoucher,
    ValidationContext,
    ValidationReport,
    validate,
)
from tallyagent_tally.xml import builders


@dataclass(slots=True)
class PendingAction:
    """A mutation that has been validated but not performed.

    Carries everything the approval UI needs to render a diff, and everything
    the executor needs to perform the write later without re-deriving it.
    """

    action_type: str
    company: str
    summary: str
    voucher: Voucher | None = None
    idempotency_key: str = ""
    validation: ValidationReport | None = None
    raw_xml: str = ""
    source: str = "chat"
    payload: dict[str, Any] = field(default_factory=dict)

    def ledger_impact(self) -> list[dict[str, str]]:
        """The debit/credit table shown to the approver.

        Always to the paisa: a figure rendered as "5000.0" in front of an
        accountant reads as a bug in the numbers, not in the formatting.
        """
        if self.voucher is None:
            return []
        from tallyagent_core.models.voucher import PAISA

        return [
            {
                "ledger": line.ledger_name,
                "debit": format(line.debit.quantize(PAISA), "f") if line.debit else "",
                "credit": format(line.credit.quantize(PAISA), "f") if line.credit else "",
            }
            for line in self.voucher.lines
        ]

    @property
    def amount(self) -> Decimal:
        return self.voucher.amount if self.voucher else Decimal("0")


@dataclass(slots=True)
class ToolResult:
    """What a tool hands back to the agent.

    Exactly one of ``data`` (a read), ``pending`` (queued for approval) or
    ``write`` (performed) is meaningful; ``message`` is always human-readable.
    """

    message: str
    data: Any = None
    pending: PendingAction | None = None
    write: WriteResult | None = None
    validation: ValidationReport | None = None

    @property
    def ok(self) -> bool:
        if self.write is not None:
            return self.write.ok
        if self.validation is not None:
            return self.validation.ok
        return True


#: Signature the approvals queue satisfies. Injected rather than imported so the
#: tools package does not depend on the approvals package.
Enqueue = Callable[[PendingAction], Awaitable[str]]


@dataclass(slots=True)
class ToolContext:
    """Everything the tools need, wired once at daemon start."""

    backend: AccountingBackend
    company: Company
    policy: Policy = field(default_factory=Policy.default)
    #: Live-mode guard rails. Default is fake mode, which permits everything.
    live: LiveMode = field(default_factory=LiveMode)
    enqueue: Enqueue | None = None
    #: Optional Tier 3 runner, wired only when the fallback is enabled. Typed as
    #: Any so the tools package does not depend on the agent package.
    fallback: Any = None
    # alias -> canonical ledger, learned from approver edits (agent memory)
    ledger_aliases: dict[str, str] = field(default_factory=dict)
    source: str = "chat"
    _masters_cache: Any = None

    async def masters(self, refresh: bool = False):  # type: ignore[no-untyped-def]
        """Masters snapshot, cached for the life of one agent turn.

        Validation needs the chart of accounts on every write; re-exporting it
        per tool call turns a five-voucher batch into five full ledger dumps.
        """
        if self._masters_cache is None or refresh:
            self._masters_cache = await self.backend.get_masters(self.company.name)
        return self._masters_cache

    async def validation_context(self) -> ValidationContext:
        masters = await self.masters()
        return ValidationContext(
            company=self.company,
            known_ledgers=masters.ledger_names,
            ledger_aliases=dict(self.ledger_aliases),
            party_state_codes={
                p.name: p.state_code for p in masters.parties if p.state_code
            },
            recent_vouchers=await self._recent_vouchers(),
            edu_mode=self.live.edu,
        )

    async def _recent_vouchers(self) -> list[PostedVoucher]:
        """Day Book rows, folded back into duplicate-detection shape.

        A Day Book row is one ledger line, so rows are grouped by voucher and
        the voucher's value is the *sum* of its debit lines - not the largest
        one. On a purchase the party sits on the credit side and the debits are
        split across the expense and the tax ledgers, so taking the largest
        single line would understate the voucher and miss the duplicate.
        """
        period = self.company.period
        if period is None:
            return []
        rows = await self.backend.get_report(
            "Day Book",
            company=self.company.name,
            from_date=period.start,
            to_date=period.end,
        )
        from tallyagent_tally.xml import parsers
        from tallyagent_tally.xml.quirks import from_tally_date

        grouped: dict[str, PostedVoucher] = {}
        for row in rows:
            number = str(row.get("VOUCHERNUMBER") or "")
            amount = parsers.to_decimal(row.get("AMOUNT"))
            key = f"{number}|{row.get('VOUCHERTYPENAME')}"
            existing = grouped.get(key)
            if existing is None:
                existing = PostedVoucher(
                    party_name=str(row.get("PARTYLEDGERNAME") or "") or None,
                    reference=str(row.get("REFERENCE") or ""),
                    amount=Decimal("0"),
                    date=from_tally_date(str(row.get("DATE") or "")) or date.min,
                    voucher_number=number,
                )
                grouped[key] = existing
            if amount > 0:
                existing.amount += amount
        return list(grouped.values())


async def submit(
    ctx: ToolContext,
    action_type: str,
    voucher: Voucher,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> ToolResult:
    """The single path every voucher write takes.

    scope -> validate -> block on failure -> auto-approve only if policy says
    so -> otherwise queue. There is deliberately no way to reach the backend's
    ``create_voucher`` from a tool without passing through here.

    The write-scope check comes first and raises rather than returning a result:
    a write aimed at a company we must not touch is a programming or config
    error, not a validation finding to be shown to an approver.
    """
    ctx.live.require_write_scope(ctx.company.name)
    report = validate(voucher, await ctx.validation_context())
    key = idempotency.make_key(ctx.company.name, voucher)
    raw_xml = builders.to_string(builders.build_voucher_element(voucher))

    pending = PendingAction(
        action_type=action_type,
        company=ctx.company.name,
        summary=summary,
        voucher=voucher,
        idempotency_key=key,
        validation=report,
        raw_xml=raw_xml,
        source=ctx.source,
        payload=payload or {},
    )

    if report.blocks_enqueue:
        return ToolResult(
            message=f"Not queued. {report.summary()}",
            pending=pending,
            validation=report,
        )

    if not ctx.policy.requires_approval(action_type, voucher.amount):
        result = await ctx.backend.create_voucher(voucher, key, ctx.company.name)
        verb = "Replayed (no-op)" if result.replayed else "Posted"
        return ToolResult(
            message=(
                f"{verb} automatically under policy: {summary}"
                if result.ok
                else f"Tally rejected it: {'; '.join(result.errors)}"
            ),
            write=result,
            validation=report,
        )

    if ctx.enqueue is None:
        return ToolResult(
            message=f"Validated, awaiting approval: {summary}",
            pending=pending,
            validation=report,
        )

    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued for approval as {ticket}: {summary}",
        data={"ticket": ticket},
        pending=pending,
        validation=report,
    )
