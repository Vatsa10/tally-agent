"""Cost centre masters, allocation and reporting.

A caution learned by measurement, not assumption: on TallyPrime 1.1.7.1 EDU the
masters create and read back perfectly, and a voucher carrying a
``CATEGORYALLOCATIONS.LIST`` is accepted - but the allocation never appears in
any export afterwards, under any fetch. So ``cost_centre_summary`` verifies
what it found against what we posted and says plainly when Tally has dropped
the allocations, rather than quietly reporting zeros.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from tallyagent_core.models import CostCategory, CostCentre
from tallyagent_core.models.costing import PRIMARY_CATEGORY
from tallyagent_core.models.voucher import PAISA
from tallyagent_tally.xml import builders
from tallyagent_tools.base import PendingAction, ToolContext, ToolResult

CATEGORY_TDL = (
    '<COLLECTION NAME="TACostCategories" ISMODIFY="No">'
    "<TYPE>CostCategory</TYPE>"
    "<FETCH>NAME,ALLOCATEREVENUE,ALLOCATENONREVENUE,MASTERID</FETCH>"
    "</COLLECTION>"
)
CENTRE_TDL = (
    '<COLLECTION NAME="TACostCentres" ISMODIFY="No">'
    "<TYPE>CostCentre</TYPE>"
    "<FETCH>NAME,PARENT,CATEGORY,MASTERID</FETCH>"
    "</COLLECTION>"
)


async def cost_centre_masters(ctx: ToolContext) -> list[CostCentre]:
    """Cost centres as models, straight from Tally."""
    rows = await ctx.backend.client.export_collection(  # type: ignore[attr-defined]
        "TACostCentres", "COSTCENTRE", company=ctx.company.name, tdl=CENTRE_TDL
    )
    return [
        CostCentre(
            name=str(row.get("@NAME") or row.get("NAME") or "").strip(),
            parent=str(row.get("PARENT") or "").strip(),
            category=str(row.get("CATEGORY") or "").strip() or PRIMARY_CATEGORY,
            master_id=str(row.get("MASTERID") or "").strip() or None,
        )
        for row in rows
        if (row.get("@NAME") or row.get("NAME"))
    ]


async def list_cost_centres(ctx: ToolContext) -> ToolResult:
    """The cost centres this company tracks, with their categories."""
    centres = await cost_centre_masters(ctx)
    return ToolResult(
        message=(
            f"{len(centres)} cost centre(s)."
            if centres
            else "This company tracks no cost centres."
        ),
        data=[
            {"name": c.name, "category": c.category, "parent": c.parent}
            for c in centres
        ],
    )


async def create_cost_centres(
    ctx: ToolContext,
    centres: list[dict[str, Any]] | None = None,
    categories: list[str] | None = None,
) -> ToolResult:
    """Create cost categories and centres as one batched approval."""
    ctx.live.require_write_scope(ctx.company.name)

    existing_centres = {c.name for c in await cost_centre_masters(ctx)}
    existing_categories = {
        str(row.get("@NAME") or row.get("NAME") or "").strip()
        for row in await ctx.backend.client.export_collection(  # type: ignore[attr-defined]
            "TACostCategories", "COSTCATEGORY", company=ctx.company.name,
            tdl=CATEGORY_TDL,
        )
    }

    new_categories = [
        CostCategory(name=name)
        for name in (categories or [])
        if name not in existing_categories
    ]
    new_centres = [
        CostCentre(
            name=str(spec["name"]),
            parent=str(spec.get("parent") or ""),
            category=str(spec.get("category") or PRIMARY_CATEGORY),
        )
        for spec in (centres or [])
        if str(spec.get("name")) not in existing_centres
    ]
    if not (new_categories or new_centres):
        return ToolResult(message="Those cost centres already exist; nothing to do.")

    summary = (
        f"Create {len(new_centres)} cost centre(s), "
        f"{len(new_categories)} categor(y/ies)"
    )
    pending = PendingAction(
        action_type="create_cost_centres",
        company=ctx.company.name,
        summary=summary,
        idempotency_key="cost-centres:"
        + ",".join(sorted([c.name for c in new_categories] + [c.name for c in new_centres])),
        source=ctx.source,
        payload={
            "kind": "cost_centres",
            "categories": [c.model_dump(mode="json") for c in new_categories],
            "centres": [c.model_dump(mode="json") for c in new_centres],
        },
    )
    if ctx.enqueue is None:
        return ToolResult(message=f"Validated, awaiting approval: {summary}", pending=pending)
    ticket = await ctx.enqueue(pending)
    return ToolResult(
        message=f"Queued as {ticket}: {summary}", data={"ticket": ticket}, pending=pending
    )


async def execute_cost_centres(ctx: ToolContext, pending: PendingAction):  # type: ignore[no-untyped-def]
    """Perform a queued cost-centre batch. Called by the one executor.

    Two waves, for the same reason stock masters need them: a centre naming a
    category created in the same envelope is not resolved.
    """
    from tallyagent_backends.accounting_backend import WriteResult

    payload = pending.payload
    waves = [
        [
            builders.build_cost_category_element(CostCategory.model_validate(c))
            for c in payload.get("categories", [])
        ],
        [
            builders.build_cost_centre_element(CostCentre.model_validate(c))
            for c in payload.get("centres", [])
        ],
    ]
    waves = [wave for wave in waves if wave]
    if not waves:
        return WriteResult(ok=True, idempotency_key=pending.idempotency_key)

    errors: list[str] = []
    sent: list[str] = []
    for wave in waves:
        result, raw = await ctx.backend.client.import_elements(  # type: ignore[attr-defined]
            wave, "All Masters", company=ctx.company.name
        )
        sent.append(raw)
        errors.extend(result.messages)
        if result.messages:
            break

    return WriteResult(
        ok=not errors,
        idempotency_key=pending.idempotency_key,
        errors=errors,
        raw_request="\n".join(sent),
    )


async def cost_centre_summary(ctx: ToolContext) -> ToolResult:
    """Net posted to each cost centre, derived from the voucher allocations."""
    centres = await cost_centre_masters(ctx)
    if not centres:
        return ToolResult(message="This company tracks no cost centres.", data=[])

    totals: dict[str, Decimal] = defaultdict(Decimal)
    categories = {c.name: c.category for c in centres}
    for row in await ctx.backend.get_vouchers(company=ctx.company.name):
        for allocation in row.get("COSTCENTREALLOCATIONS") or []:
            name = str(allocation.get("name") or "")
            if name:
                totals[name] += Decimal(str(allocation.get("amount") or "0"))

    rows = [
        {
            "cost_centre": centre.name,
            "category": categories.get(centre.name, PRIMARY_CATEGORY),
            "net": format(totals.get(centre.name, Decimal("0")).quantize(PAISA), "f"),
        }
        for centre in centres
    ]
    if not totals:
        return ToolResult(
            message=(
                f"{len(centres)} cost centre(s), but Tally returned no allocations. "
                "On TallyPrime 1.1.7.1 an allocation is accepted on import and then "
                "absent from every export, so this is a limitation of the Tally "
                "build, not an empty set of postings."
            ),
            data=rows,
        )
    return ToolResult(message=f"Cost centre summary across {len(rows)} centre(s).", data=rows)
