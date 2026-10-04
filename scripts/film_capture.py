"""Capture what the product actually says, feature by feature, for the film.

    uv run python scripts/film_capture.py

The product video is drawn rather than screen-recorded, which makes it sharp
and re-renderable - and makes it easy to lie. So nothing on screen is written by
hand: every line the film shows a terminal printing is a line the product
printed, here, against the live TallyPrime, captured into
``film/data/capture.json``. Re-run this after a change and the film shows the
new behaviour.

Runs in-process - no keyboard, no windows - so it can run while someone is
using the machine.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

from tallyagent_channels.tui.session import Session
from tallyagent_core import dotenv
from tallyagent_daemon import clients, wiring
from tallyagent_daemon import config as config_mod
from tallyagent_daemon.firm import brief
from tallyagent_daemon.firm.setup import make_runner

OUT = Path("film/data/capture.json")
RUN_DAY = date(2026, 7, 15)


async def main() -> int:
    dotenv.load()
    base = config_mod.load("config/config.toml", "config/policy.toml")
    register = clients.load("config/clients.toml")
    demo = register.get("demo")
    wired = wiring.build(clients.apply(base, demo, register.data_dir))

    def switch(slug: str):  # type: ignore[no-untyped-def]
        return wiring.build(clients.apply(base, register.get(slug), register.data_dir)).services

    runner = make_runner(base, register)
    session = Session(wired.services, live=base.live, clients=register, switch=switch,
                      firm=runner)
    session.client_slug = "demo"
    stamp = int(time.time()) % 100000
    scenes: dict[str, list[dict[str, object]]] = {}

    async def say(scene: str, line: str) -> list[dict[str, object]]:
        started = time.monotonic()
        turn = await session.handle(line)
        took = round(time.monotonic() - started, 1)
        lines = [
            {
                "kind": out.kind,
                "text": out.text,
                "tool": out.meta.get("tool", ""),
                "detail": str(out.detail or "")[:300],
            }
            for out in turn.lines
        ]
        # The PIN never goes into the film, even though it went into the session.
        for out in lines:
            if out["kind"] == "user" and str(out["text"]).startswith("/signin"):
                parts = str(out["text"]).split()
                out["text"] = " ".join(parts[:-1] + ["****"])
        scenes.setdefault(scene, []).append({"input": lines[0]["text"] if lines else line,
                                             "seconds": took, "lines": lines[1:]})
        print(f"[{scene}] {line[:60]}  ({took}s)")
        return lines

    # 1. who is at the keyboard
    await say("people", "/users")
    await say("people", "/signin Nikhil 1199")

    # 2. a sale, drafted by a clerk, refused, approved by a partner
    sale = await say(
        "sale",
        "raise a sales invoice to Acme Industries for 2 Widget at 3200 each, 18% GST, "
        f"dated 2 June 2026, reference FILM-{stamp}",
    )
    ticket = next((str(t) for line in sale for t in [line["text"]]
                   if line["kind"] == "approval" and "APR-" in str(t)), "")
    ticket = ticket[ticket.index("APR-"):ticket.index("APR-") + 8] if "APR-" in ticket else ""
    await say("sale", f"/approve {ticket}")
    await say("sale", "/signin R. Mehta 4821")
    await say("sale", f"/approve {ticket}")

    # 3. what it refuses
    await say("refusal", "book 100 rent to the Baroda branch")

    # 4. many clients, and consent
    await say("clients", "/client")
    await say("clients", "/consent")
    await say("clients", "/client sharma")
    await say("clients", "/consent")
    await say("clients", "/client demo")

    # 5. reports read live
    await say("reports", "trial balance please")
    await say("reports", "who owes us money?")

    # 6. the bill pile
    await say("bills", "/bills demo/bills --limit 3 --again")

    # 7. the morning run across every client
    report = await runner.run(RUN_DAY, force=True)
    brief_path = brief.write(report)
    scenes["firm"] = [{"input": "/run --force", "seconds": 0, "lines": [
        {"kind": "system", "text": report.summary(), "tool": "", "detail": ""}]}]
    await say("firm", "/inbox")
    await say("firm", "/autonomy")

    # 8. month end and GSTR-1
    await say("close", "/monthend 2026-06 tests/fixtures/live_bank_statement.csv "
                       "tests/fixtures/live_gstr2b.json")
    await say("close", "/gstr1 2026-06")

    # 9. the audit chain
    entries = wired.services.audit.entries()
    audit = [
        {"seq": e.sequence, "at": e.at.isoformat(timespec="seconds"), "event": e.event,
         "actor": e.actor}
        for e in entries[-12:]
    ]
    verified = wired.services.audit.verify()

    files: dict[str, str] = {}
    for name, path in {
        "brief": brief_path,
        "followup": Path("reports/reco/TA-Demo Traders/2026-06/followups/Bharat Supplies.txt"),
        "accuracy_photo": Path("reports/bill_accuracy_photo.md"),
        "close_pack": Path("reports/close/TA-Demo Traders/2026-06/close.md"),
    }.items():
        files[name] = path.read_text(encoding="utf-8") if path.is_file() else ""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "captured": date.today().isoformat(),
        "company": demo.company,
        "model": wired.services.router.name,
        "scenes": scenes,
        "audit": audit,
        "audit_ok": verified.ok,
        "audit_count": len(entries),
        "files": files,
        "run": {
            "summary": report.summary(),
            "clients": [
                {"slug": c.slug, "company": c.company, "healthy": c.healthy,
                 "reason": c.reason, "done": c.count("done"), "queued": c.count("queued"),
                 "exceptions": c.count("exception")}
                for c in report.clients
            ],
        },
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"captured {sum(len(v) for v in scenes.values())} turns -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
