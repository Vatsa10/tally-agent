"""The runner's half of earned autonomy: turn decisions into inbox lines.

Kept apart from the rules themselves (``tallyagent_approvals.autonomy``) so the
rules can be tested on queue rows alone, and so this is the single place the
morning run lets anything into the books without a person.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from tallyagent_approvals import autonomy as rules
from tallyagent_daemon.firm.jobs import DONE, EXCEPTION, QUEUED, Outcome


class EarnedAutonomy:
    name = "autonomy"

    async def apply(self, wired: Any, client: Any, today: date) -> list[Outcome]:
        services = wired.services
        grants = getattr(services, "autonomy", None)
        if grants is None:
            return []
        decisions = await rules.decide(services.queue, grants, client.company)
        outcomes: list[Outcome] = []
        for decision in decisions:
            if decision.posted:
                kind, title = DONE, (
                    f"Posted {decision.action_type.replace('_', ' ')} "
                    f"Rs {decision.amount:,.2f} under earned autonomy"
                    + (f" as {decision.voucher_number}" if decision.voucher_number else "")
                )
            elif decision.errors:
                kind, title = EXCEPTION, (
                    f"{decision.ticket}: the autonomous post failed - "
                    f"{'; '.join(decision.errors)}"
                )
            else:
                kind, title = QUEUED, (
                    f"{decision.ticket} waiting: {decision.action_type.replace('_', ' ')} "
                    f"Rs {decision.amount:,.2f}"
                )
            outcomes.append(
                Outcome(
                    job=self.name,
                    kind=kind,
                    title=title,
                    detail=decision.reason,
                    ticket=decision.ticket,
                    subject=decision.ticket,
                )
            )
        metric = rules.no_touch(services.queue, services.audit, client.company)
        if metric.posted:
            outcomes.append(
                Outcome(
                    job=self.name,
                    kind=DONE,
                    title=f"No-touch rate: {metric.describe()}",
                    subject="no-touch",
                    data={"rate": metric.rate},
                )
            )
        return outcomes
