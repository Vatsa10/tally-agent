"""The one function that turns an approved action into a Tally write.

There is exactly one of these on purpose. It used to be a closure in
``daemon.wiring`` with near-copies in every test fixture, which meant a new
action type (deletion, say) worked in production and silently did nothing
everywhere else - the tests would still pass while proving less than they
looked like they proved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date

from tallyagent_backends.accounting_backend import WriteResult
from tallyagent_core.models import VoucherType
from tallyagent_tools import masters
from tallyagent_tools.base import PendingAction, ToolContext

Executor = Callable[[PendingAction], Awaitable[WriteResult]]


async def execute(ctx: ToolContext, action: PendingAction) -> WriteResult:
    """Perform one approved action against the backend.

    Dispatch is on what the action *is*, not on which surface queued it, so a
    deletion approved in the TUI, the web UI or over MCP takes the identical
    path.
    """
    backend = ctx.backend
    company = ctx.company.name

    if action.payload.get("kind") == "delete_voucher":
        remote_id = str(action.payload["remote_id"])
        result = await backend.delete_voucher(  # type: ignore[attr-defined]
            remote_id,
            VoucherType(str(action.payload["voucher_type"])),
            date.fromisoformat(str(action.payload["date"])),
            action.idempotency_key,
            company,
            master_id=str(action.payload.get("master_id", "")),
        )
        if result.ok and ctx.voucher_index is not None:
            ctx.voucher_index.mark_deleted(remote_id)
        return result

    if action.voucher is None:
        return await masters._execute_master(ctx, action)

    if action.action_type == "alter_voucher":
        result = await backend.alter_voucher(  # type: ignore[attr-defined]
            action.voucher,
            str(action.payload.get("remote_id", "")),
            action.idempotency_key,
            company,
        )
        if result.ok and ctx.voucher_index is not None:
            # The amendment keeps the original identity; refresh what we know.
            ctx.voucher_index.record(
                remote_id=str(action.payload.get("remote_id", "")),
                voucher_type=action.voucher.voucher_type.value,
                reference=action.voucher.reference,
                voucher_date=action.voucher.date.isoformat(),
                amount=format(action.voucher.amount, "f"),
            )
        return result

    result = await backend.create_voucher(
        action.voucher, action.idempotency_key, company
    )
    _remember(ctx, action, result)
    return result


def _remember(ctx: ToolContext, action: PendingAction, result: WriteResult) -> None:
    """Record the identity we gave the voucher, so it stays amendable.

    Tally honours a supplied REMOTEID but never reports it back, so this is the
    only record that a given voucher is ours to change.
    """
    if not (result.ok and result.remote_id and ctx.voucher_index is not None):
        return
    voucher = action.voucher
    ctx.voucher_index.record(
        remote_id=result.remote_id,
        master_id=result.master_id,
        voucher_type=voucher.voucher_type.value if voucher else "",
        reference=voucher.reference if voucher else "",
        voucher_date=voucher.date.isoformat() if voucher else "",
        amount=format(voucher.amount, "f") if voucher else "0",
    )


def build_executor(ctx: ToolContext) -> Executor:
    """Bind ``execute`` to a tool context, for the approval queue."""

    async def run(action: PendingAction) -> WriteResult:
        return await execute(ctx, action)

    return run
