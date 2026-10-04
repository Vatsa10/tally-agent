"""The morning brief: what the run did, client by client, in a page.

The inbox is the list to work through; the brief is what a partner reads with
the first coffee - or gets on WhatsApp - to know whether today is a normal day.
"""

from __future__ import annotations

from pathlib import Path

from tallyagent_daemon.firm.inbox import RANK
from tallyagent_daemon.firm.jobs import DONE, EXCEPTION, QUEUED
from tallyagent_daemon.firm.runner import RunReport


def markdown(report: RunReport) -> str:
    lines = [
        f"# Morning brief - {report.day:%d %b %Y}",
        "",
        report.summary(),
        "",
        "| Client | Done | Waiting | Needs a person | At risk |",
        "|---|---|---|---|---|",
    ]
    for client in report.clients:
        risk = f"Rs {client.at_risk:,.2f}" if client.at_risk else ""
        state = "" if client.healthy else " (could not run)"
        lines.append(
            f"| {client.slug}{state} | {client.count(DONE)} | {client.count(QUEUED)} "
            f"| {client.count(EXCEPTION)} | {risk} |"
        )
    lines.append("")

    needs = sorted(
        (o for o in report.outcomes if o.kind != DONE),
        key=lambda o: (RANK.get(o.kind, 9), -o.amount_at_risk),
    )
    if needs:
        lines += ["## Needs a person, worst first", ""]
        for outcome in needs[:25]:
            money = f" - Rs {outcome.amount_at_risk:,.2f}" if outcome.amount_at_risk else ""
            ticket = f" ({outcome.ticket})" if outcome.ticket else ""
            lines.append(f"- **{outcome.client}** - {outcome.title}{money}{ticket}")
            if outcome.detail:
                lines.append(f"  {outcome.detail}")
        if len(needs) > 25:
            lines.append(f"- ... and {len(needs) - 25} more in the inbox")
        lines.append("")
    else:
        lines += ["Nothing needs a person today.", ""]
    return "\n".join(lines)


def whatsapp(report: RunReport) -> str:
    """The same, short enough to read on a phone."""
    top = [o for o in report.outcomes if o.kind == EXCEPTION][:3]
    text = [f"Morning run {report.day:%d %b}: {report.summary()}"]
    text += [f"- {o.client}: {o.title}" for o in top]
    return "\n".join(text)


def write(report: RunReport, folder: str | Path = "reports/brief") -> Path:
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{report.day.isoformat()}.md"
    path.write_text(markdown(report), encoding="utf-8")
    return path
