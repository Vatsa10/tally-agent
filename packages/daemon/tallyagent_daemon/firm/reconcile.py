"""The monthly GSTR-2B job: reconcile, explain, chase, and draft what is missing.

GSTR-2B for a month is generated on the 14th of the next, and GSTR-3B is due on
the 20th. In that window, for every client, this:

1. reads the client's 2B download for last month;
2. matches it against the purchase register on supplier GSTIN and invoice
   number, and gives every invoice a cause and an action (``tallyagent_tools.itc``);
3. carries unfiled invoices on the deferred-ITC ledger, and says which earlier
   ones have now become claimable or are near their section 16(4) deadline;
4. drafts a follow-up message to every supplier who has not filed;
5. writes IMS suggestions - accept or keep pending, each with a reason;
6. drafts purchase vouchers for bills that are in 2B but nobody keyed in, where
   the supplier is already a ledger - which then go to approval, or post under
   earned autonomy if that kind of entry has earned it.

The 2B file comes from the portal; downloading it needs the client's GST login,
which this does not hold. When it is missing, the outcome says exactly where to
put it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from tallyagent_daemon.firm.jobs import DONE, EXCEPTION, QUEUED, Outcome, previous_month

log = logging.getLogger(__name__)

#: GST slabs, for turning a 2B invoice's tax back into a rate.
SLABS = (Decimal("0"), Decimal("5"), Decimal("12"), Decimal("18"), Decimal("28"))


def gstr2b_folder(client: Any) -> Path:
    return Path(getattr(client, "gstr2b_dir", "") or f"clients/{client.slug}/gstr2b")


def find_2b(folder: Path, month: str) -> tuple[Path, dict[str, Any]] | None:
    """The 2B download for ``month`` (YYYY-MM) in a folder, by its own period.

    Files are matched on the ``rtnprd`` inside them rather than on their names,
    because the portal's download names say nothing useful and people rename
    them anyway.
    """
    wanted = f"{month[5:7]}{month[0:4]}"
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("could not read %s as JSON", path)
            continue
        data = document.get("data", document)
        if str(data.get("rtnprd") or document.get("rtnprd") or "") == wanted:
            return path, document
    return None


def nearest_slab(taxable: Decimal, tax: Decimal) -> Decimal:
    if not taxable:
        return Decimal("0")
    rate = tax / taxable * 100
    return min(SLABS, key=lambda slab: abs(slab - rate))


@dataclass(slots=True)
class Reconcile2BJob:
    name: str = "gstr2b"
    #: 2B is generated on the 14th; 3B is due on the 20th.
    window: tuple[int, int] = (14, 31)
    out_dir: str = "reports/reco"

    def due(self, client: Any, today: date) -> bool:
        return self.window[0] <= today.day <= self.window[1]

    async def run(self, ctx: Any, client: Any, today: date) -> list[Outcome]:
        from tallyagent_tools import close, itc, reports

        month = previous_month(today)
        start, end = close.month_dates(month)
        due_3b = today.replace(day=20) if today.day <= 20 else None
        folder = gstr2b_folder(client)
        found = find_2b(folder, month)
        if found is None:
            return [
                Outcome(
                    job=self.name,
                    kind=EXCEPTION,
                    title=f"GSTR-2B for {month} has not been downloaded",
                    detail=(
                        f"Download it from the GST portal (Returns > GSTR-2B > JSON) "
                        f"and put it in {folder}. The rest of the reconciliation "
                        "runs on its own after that."
                    ),
                    due=due_3b,
                    subject=f"{month}:missing",
                )
            ]
        path, document = found
        period, portal = itc.parse_2b(document)

        masters = await ctx.masters()
        gstins = {p.name: (p.gstin or "") for p in masters.parties}
        register = await reports.gstr2_purchase_register(
            ctx, from_date=start - itc.LOOKBACK, to_date=end
        )
        books = itc.book_invoices(register.data or [], gstins)
        result = itc.reconcile(period, portal, books)

        outcomes = [
            Outcome(
                job=self.name,
                kind=DONE,
                title=result.summary(),
                detail=str(path),
                subject=f"{month}:summary",
                data={"counts": result.counts()},
            )
        ]

        folder_out = self._write(ctx, client, month, result)

        # 3. the deferred ledger
        engine = _engine_of(ctx)
        if engine is not None:
            from tallyagent_approvals.deferred_itc import DeferredItcLedger

            update = DeferredItcLedger(engine).update(client.company, result, today)
            if update.now_claimable:
                outcomes.append(
                    Outcome(
                        job=self.name,
                        kind=DONE,
                        title=(
                            f"Rs {update.claimable_tax:,.2f} of deferred ITC is now "
                            f"claimable in {month}"
                        ),
                        detail=", ".join(
                            f"{d.supplier} {d.invoice_no}" for d in update.now_claimable[:8]
                        ),
                        subject=f"{month}:claimable",
                    )
                )
            for item, deadline in update.near_deadline:
                outcomes.append(
                    Outcome(
                        job=self.name,
                        kind=EXCEPTION,
                        title=(
                            f"{item.supplier} {item.invoice_no}: Rs {item.tax:,.2f} of ITC "
                            f"lapses on {deadline:%d %b %Y}"
                        ),
                        detail="Still not in 2B. After this date it cannot be claimed at all.",
                        amount_at_risk=item.tax,
                        due=deadline,
                        subject=f"deadline:{item.supplier_gstin}:{item.invoice_no}",
                    )
                )

        # 4. one line per supplier to chase, with the message ready
        by_supplier: dict[str, list[Any]] = {}
        for finding in result.by(itc.NOT_FILED):
            by_supplier.setdefault(finding.supplier, []).append(finding)
        for supplier, items in sorted(by_supplier.items()):
            at_risk = sum((f.itc_at_risk for f in items), Decimal("0"))
            outcomes.append(
                Outcome(
                    job=self.name,
                    kind=EXCEPTION,
                    title=f"{supplier} has not filed {len(items)} invoice(s)",
                    detail=(
                        "Credit deferred until they do. Follow-up drafted: "
                        f"{folder_out / 'followups' / _safe(supplier)}.txt"
                    ),
                    amount_at_risk=at_risk,
                    due=due_3b,
                    subject=f"{month}:notfiled:{supplier}",
                )
            )

        # every other cause that needs a person, one line each
        for finding in result.findings:
            if finding.cause in (itc.MATCHED, itc.NOT_FILED, itc.NOT_IN_BOOKS):
                continue
            outcomes.append(
                Outcome(
                    job=self.name,
                    kind=EXCEPTION,
                    title=(
                        f"{finding.supplier} {finding.number}: "
                        f"{itc.CAUSE_WORDS[finding.cause]}"
                    ),
                    detail=f"{finding.explanation} {finding.action}",
                    amount_at_risk=finding.itc_at_risk,
                    due=due_3b,
                    subject=f"{month}:{finding.cause}:{finding.supplier_gstin}:{finding.number}",
                )
            )

        # 6. bills in 2B nobody keyed in
        known = {g.upper(): name for name, g in gstins.items() if g}
        for finding in result.by(itc.NOT_IN_BOOKS):
            outcomes.append(await self._draft_bill(ctx, finding, known, month))

        # 5. IMS
        actions = itc.ims_actions(result)
        pending = sum(1 for a in actions if a["suggestion"] == "pending")
        outcomes.append(
            Outcome(
                job=self.name,
                kind=DONE,
                title=(
                    f"IMS suggestions for {month}: {len(actions) - pending} to accept, "
                    f"{pending} to keep pending"
                ),
                detail=(
                    f"{folder_out / 'ims.csv'} - each with its reason. Doing nothing in "
                    "IMS counts as accepting, so the pending ones need a decision."
                ),
                subject=f"{month}:ims",
            )
        )
        return outcomes

    async def _draft_bill(
        self, ctx: Any, finding: Any, known: dict[str, str], month: str
    ) -> Outcome:
        from tallyagent_tools import vouchers

        subject = f"{month}:notinbooks:{finding.supplier_gstin}:{finding.number}"
        party = known.get(finding.supplier_gstin.upper())
        inv = finding.portal
        if party is None or inv is None:
            return Outcome(
                job=self.name,
                kind=EXCEPTION,
                title=f"{finding.supplier} {finding.number}: in 2B, not in the books",
                detail=(
                    f"{finding.supplier} ({finding.supplier_gstin}) is not a ledger here. "
                    "Find the bill; create the supplier only if it is genuinely this "
                    "client's purchase."
                ),
                subject=subject,
            )
        rate = nearest_slab(inv.taxable, inv.tax)
        try:
            result = await vouchers.create_purchase_voucher(
                ctx,
                party_name=party,
                taxable_value=inv.taxable,
                gst_rate=rate,
                voucher_date=inv.invoice_date,
                reference=finding.number,
                narration=f"From GSTR-2B {month}: {finding.supplier} {finding.number}",
                place_of_supply=finding.supplier_gstin[:2] or None,
            )
        except Exception as exc:  # noqa: BLE001 - a draft that cannot be built is a line
            return Outcome(
                job=self.name,
                kind=EXCEPTION,
                title=f"{finding.supplier} {finding.number}: could not draft the bill",
                detail=f"{type(exc).__name__}: {exc}",
                subject=subject,
            )
        data = result.data if isinstance(result.data, dict) else {}
        ticket = str(data.get("ticket") or "")
        if result.write is not None and result.write.ok:
            kind = DONE
            title = f"Posted missing bill {finding.supplier} {finding.number} under policy"
        elif ticket:
            kind, title = QUEUED, (
                f"Drafted missing bill {finding.supplier} {finding.number} "
                f"(Rs {inv.total:,.2f}) from 2B"
            )
        else:
            kind, title = EXCEPTION, (
                f"{finding.supplier} {finding.number}: drafted from 2B but not queued"
            )
        return Outcome(
            job=self.name,
            kind=kind,
            title=title,
            detail=result.message,
            ticket=ticket,
            subject=subject,
        )

    def _write(self, ctx: Any, client: Any, month: str, result: Any) -> Path:
        from tallyagent_tools import itc
        from tallyagent_tools.gstr1 import folder_name

        folder = Path(self.out_dir) / folder_name(client.company) / month
        (folder / "followups").mkdir(parents=True, exist_ok=True)
        (folder / "reco.csv").write_text(result.csv(), encoding="utf-8")
        actions = itc.ims_actions(result)
        lines = ["supplier_gstin,supplier,invoice_no,suggestion,reason"]
        lines += [
            ",".join(
                '"' + str(a[k]).replace('"', "'") + '"'
                for k in ("supplier_gstin", "supplier", "invoice_no", "suggestion", "reason")
            )
            for a in actions
        ]
        (folder / "ims.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for supplier, text in itc.followups(result, client=client.company).items():
            (folder / "followups" / f"{_safe(supplier)}.txt").write_text(text, encoding="utf-8")
        (folder / "summary.md").write_text(
            f"# GSTR-2B reconciliation - {client.company} - {month}\n\n{result.summary()}\n",
            encoding="utf-8",
        )
        return folder


def _safe(name: str) -> str:
    from tallyagent_tools.gstr1 import folder_name

    return folder_name(name)


def _engine_of(ctx: Any) -> Any:
    """The client's own database - the same one its audit chain is written to,
    so the deferred ledger can never end up in another client's file."""
    audit = getattr(ctx, "audit", None)
    return getattr(audit, "engine", None)
