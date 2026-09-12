"""Currency masters, and the refusal that keeps Tally alive.

Importing a ``<CURRENCY>`` element terminates TallyPrime 1.1.7.1 - the process
dies mid-request, the port stops answering, and the company has to be reopened
through the licence screen. Measured twice, deliberately not a third time.

So every write here checks ``[tally] supports_multi_currency`` first and
refuses with an explanation. The code is complete for a build that does support
currencies; it simply will not be pointed at one that does not.
"""

from __future__ import annotations

from typing import Any

from tallyagent_core.models import Currency
from tallyagent_tally.xml import builders
from tallyagent_tools.base import PendingAction, ToolContext, ToolResult

CURRENCY_TDL = (
    '<COLLECTION NAME="TACurrencies" ISMODIFY="No">'
    "<TYPE>Currency</TYPE>"
    "<FETCH>NAME,MAILINGNAME,DECIMALPLACES,MASTERID</FETCH>"
    "</COLLECTION>"
)

REFUSAL = (
    "This TallyPrime cannot handle foreign currency: importing a currency "
    "master terminates the process on 1.1.7.1 (measured). Nothing was sent. "
    "Set [tally] supports_multi_currency = true for a build that handles it."
)


def _supported(ctx: ToolContext) -> bool:
    return bool(getattr(ctx.backend.client.config, "supports_multi_currency", False))


async def currency_masters(ctx: ToolContext) -> list[Currency]:
    """Currency masters as models. Reading is safe on every build."""
    rows = await ctx.backend.client.export_collection(  # type: ignore[attr-defined]
        "TACurrencies", "CURRENCY", company=ctx.company.name, tdl=CURRENCY_TDL
    )
    return [
        Currency(
            name=str(row.get("@NAME") or row.get("NAME") or "").strip(),
            formal_name=str(row.get("MAILINGNAME") or "").strip(),
            decimal_places=int(str(row.get("DECIMALPLACES") or "2").strip() or 2),
            master_id=str(row.get("MASTERID") or "").strip() or None,
        )
        for row in rows
        if (row.get("@NAME") or row.get("NAME"))
    ]


async def list_currencies(ctx: ToolContext) -> ToolResult:
    """The currencies this company has masters for."""
    found = await currency_masters(ctx)
    return ToolResult(
        message=f"{len(found)} currency master(s).",
        data=[{"symbol": c.name, "name": c.formal_name} for c in found],
    )


async def create_currencies(
    ctx: ToolContext, currencies: list[dict[str, Any]] | None = None
) -> ToolResult:
    """Create currency masters, if this Tally can survive it."""
    ctx.live.require_write_scope(ctx.company.name)
    if not _supported(ctx):
        return ToolResult(message=REFUSAL)

    existing = {c.name for c in await currency_masters(ctx)}
    wanted = [
        Currency(
            name=str(spec["symbol"]),
            formal_name=str(spec.get("name") or ""),
            decimal_places=int(spec.get("decimal_places") or 2),
        )
        for spec in (currencies or [])
        if str(spec.get("symbol")) not in existing
    ]
    if not wanted:
        return ToolResult(message="Those currencies already exist; nothing to do.")

    summary = "Create " + ", ".join(c.name for c in wanted)
    pending = PendingAction(
        action_type="create_currencies",
        company=ctx.company.name,
        summary=summary,
        idempotency_key="currencies:" + ",".join(sorted(c.name for c in wanted)),
        source=ctx.source,
        payload={
            "kind": "currencies",
            "currencies": [c.model_dump(mode="json") for c in wanted],
        },
    )
    if ctx.enqueue is None:
        return ToolResult(
            message=f"Validated, awaiting approval: {summary}", pending=pending
        )
    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued as {ticket}: {summary}",
        data={"ticket": ticket},
        pending=pending,
    )


async def execute_currencies(ctx: ToolContext, pending: PendingAction):  # type: ignore[no-untyped-def]
    """Perform a queued currency batch. Called by the one executor."""
    from tallyagent_backends.accounting_backend import WriteResult

    if not _supported(ctx):
        # Belt and braces: an approval could have been queued before the flag
        # was turned off, and this is the call that would kill Tally.
        return WriteResult(
            ok=False, idempotency_key=pending.idempotency_key, errors=[REFUSAL]
        )

    elements = [
        builders.build_currency_element(Currency.model_validate(c))
        for c in pending.payload.get("currencies", [])
    ]
    if not elements:
        return WriteResult(ok=True, idempotency_key=pending.idempotency_key)

    result, raw = await ctx.backend.client.import_elements(  # type: ignore[attr-defined]
        elements, "All Masters", company=ctx.company.name
    )
    return WriteResult(
        ok=result.ok,
        master_id=result.last_master_id,
        idempotency_key=pending.idempotency_key,
        errors=result.messages,
        raw_request=raw,
    )
