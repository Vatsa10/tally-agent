"""The ``tallyagent`` command line.

    tallyagent probe             is Tally there, which edition, what is loaded
    tallyagent tui               the terminal chat UI (the main control surface)
    tallyagent chat              a plain REPL, or --script for a scenario
    tallyagent enable-server     turn on Tally's XML interface and restart it
    tallyagent serve             the daemon: web UI, WhatsApp webhook, scheduler
    tallyagent mcp               the MCP server (stdio or http)
    tallyagent audit verify      check the hash chain
    tallyagent approvals ...     list, stats, approve, reject
    tallyagent fake-tally        run the fake Tally for the demo
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import replace
from pathlib import Path

import typer

from tallyagent_daemon import wiring
from tallyagent_daemon.config import Config, load

app = typer.Typer(
    help="Agentic bookkeeping for TallyPrime.", no_args_is_help=True, add_completion=False
)
approvals_app = typer.Typer(help="Inspect and decide queued actions.")
audit_app = typer.Typer(help="The append-only audit log.")
app.add_typer(approvals_app, name="approvals")
app.add_typer(audit_app, name="audit")

CONFIG_OPTION = typer.Option("config/config.toml", "--config", "-c", help="Path to config.toml.")
POLICY_OPTION = typer.Option("config/policy.toml", "--policy", help="Path to policy.toml.")
FAKE_OPTION = typer.Option(
    False, "--fake-tally", help="Run against the in-process fake Tally instead of a real one."
)


def _load(config_path: str, policy_path: str) -> Config:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    return load(config_path, policy_path)


def _wire(config: Config, fake: bool) -> wiring.Wired:
    """Attach the fake Tally when asked, so the demo needs no TallyPrime."""
    transport = None
    if fake:
        from tallyagent_tally.fake_server import seeded_demo

        tally = seeded_demo(config.company.name or "Demo Traders Pvt Ltd")
        # Report an honest address: the fake is in-process, not at the
        # placeholder host still sitting in config.toml.
        config.tally.host = "fake-tally"
        # The fake cannot touch real books, so the live guard does not apply -
        # leaving it on would refuse to write to the fake's demo company.
        config.mode = "fake"
        config.live = replace(config.live, enabled=False)
        if not config.company.name:
            config.company = config.company.model_copy(
                update={"name": "Demo Traders Pvt Ltd"}
            )
            config.tally.company = config.company.name
        transport = tally.transport
    return wiring.build(config, transport=transport)


def _wire_fallback(wired: wiring.Wired) -> None:
    """Attach the Tier 3 runner when policy allows it.

    Wired here rather than in ``wiring.build`` so the fallback only exists in
    the surfaces that can actually ask a human to approve a keystroke.
    """
    config = wired.config
    if not config.tiers.fallback_enabled:
        return
    from tallyagent_agent.fallback import spotlight as spotlight_mod
    from tallyagent_agent.fallback import window as window_mod
    from tallyagent_agent.fallback.computer_use import ComputerUseFallback
    from tallyagent_agent.tiers import TierRouter

    router = TierRouter(config.tiers)
    wired.services.tiers = router  # type: ignore[attr-defined]
    wired.services.tools.fallback = ComputerUseFallback(
        wired.services.router,
        router,
        spotlight=spotlight_mod.build(config.tiers.show_cursor),
        # Tally is brought up and put in front before a single key is sent,
        # whether the request came from the TUI, the web UI or a chat.
        ensure_visible=window_mod.ensure_visible,
    )


@app.command()
def probe(
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
    fake: bool = FAKE_OPTION,
) -> None:
    """Check the Tally connection, edition, server mode and open companies."""
    config = _load(config_path, policy_path)
    if config.tally_is_placeholder and not fake:
        typer.echo(
            f"[tally] host is still the placeholder {config.tally.host!r}. Edit "
            f"{config_path} and set it to 127.0.0.1 (or your Tally machine), or "
            "run with --fake-tally.",
            err=True,
        )
        raise typer.Exit(code=2)

    wired = _wire(config, fake)

    from tallyagent_tally.probe import probe as run_probe

    report = asyncio.run(run_probe(wired.backend.client, config.live))
    typer.echo(report.render())
    if not report.ready:
        raise typer.Exit(code=1)


@app.command()
def chat(
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
    fake: bool = FAKE_OPTION,
    script: str = typer.Option(
        "", "--script", help="Run a YAML scenario non-interactively instead of a REPL."
    ),
) -> None:
    """A REPL. Type a question; 'quit' to leave. --script runs a scenario."""
    config = _load(config_path, policy_path)
    wired = _wire(config, fake)
    services = wired.services

    if script:
        from tallyagent_channels.tui.script_runner import Scenario, run_scenario

        scenario = Scenario.load(script)
        result = asyncio.run(run_scenario(services, scenario, live=config.live))
        typer.echo(result.report())
        if not result.ok:
            raise typer.Exit(code=1)
        return

    typer.echo(f"tallyagent - {services.company.name or '(no company configured)'}")
    if wired.using_mock_model:
        typer.echo(
            "No model API key found, so this session uses the deterministic mock "
            "provider. Set DEEPSEEK_API_KEY for the real thing."
        )
    typer.echo("Writes are queued for approval; nothing posts from here.\n")

    agent = services.agent("cli")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            typer.echo("")
            break
        if not line:
            continue
        if line.lower() in ("quit", "exit", ":q"):
            break

        result = asyncio.run(agent.run(line))
        typer.echo(result.text)
        if result.tickets:
            typer.echo(
                f"  queued for approval: {', '.join(result.tickets)} "
                "(nothing posted yet)"
            )
        typer.echo(
            f"  [{len(result.steps)} step(s), {result.total_tokens} tokens, "
            f"{result.total_egress_bytes} bytes sent]"
        )


@app.command()
def tui(
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
    fake: bool = FAKE_OPTION,
) -> None:
    """The terminal UI: chat, Tally status, and the approval queue."""
    from tallyagent_channels.tui.app import run as run_tui

    config = _load(config_path, policy_path)
    # The TUI owns the screen, so logging must not scribble over it.
    logging.getLogger().handlers.clear()
    logging.getLogger().addHandler(logging.NullHandler())

    wired = _wire(config, fake)
    _wire_fallback(wired)
    run_tui(wired.services, config.live)


@app.command("enable-server")
def enable_server(
    port: int = typer.Option(9000, help="The port Tally should listen on."),
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
) -> None:
    """Turn on TallyPrime's XML interface, restart it, and verify the port."""
    from tallyagent_tools import tally_admin

    config = _load(config_path, policy_path)
    wired = _wire(config, fake=False)
    _wire_fallback(wired)
    result = asyncio.run(
        tally_admin.enable_tally_server(wired.services.tools, port=port)
    )
    typer.echo(result.message)
    if not (result.data or {}).get("ok"):
        raise typer.Exit(code=1)


@app.command()
def serve(
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
    fake: bool = FAKE_OPTION,
) -> None:
    """Run the daemon: web UI, WhatsApp webhook, scheduler, tray icon."""
    import uvicorn

    from tallyagent_daemon.app import build_app
    from tallyagent_daemon.tray import run_tray

    config = _load(config_path, policy_path)
    wired = _wire(config, fake)
    url = f"http://{config.daemon.host}:{config.daemon.port}/"

    if config.daemon.tray:
        run_tray(url, wired.services.status)

    typer.echo(f"tallyagent is at {url}")
    if wired.using_mock_model:
        typer.echo("Using the mock model provider (no API key configured).")
    uvicorn.run(
        build_app(wired),
        host=config.daemon.host,
        port=config.daemon.port,
        log_level=config.logging.level.lower(),
    )


@app.command()
def mcp(
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
    fake: bool = FAKE_OPTION,
    transport: str = typer.Option("", help="stdio or http. Defaults to config."),
    read_only: bool = typer.Option(False, help="Expose read tools only."),
) -> None:
    """Serve the tool registry over MCP."""
    from tallyagent_mcp_server import server as mcp_server

    config = _load(config_path, policy_path)
    wired = _wire(config, fake)
    kind = transport or config.mcp.transport
    only_reads = read_only or config.mcp.read_only

    if kind == "stdio":
        asyncio.run(mcp_server.run_stdio(wired.services.tools, read_only=only_reads))
        return
    if kind == "http":
        import uvicorn

        application = mcp_server.build_http_app(
            wired.services.tools, read_only=only_reads
        )
        uvicorn.run(
            application, host=config.daemon.host, port=config.daemon.port + 1
        )
        return
    typer.echo(f"unknown MCP transport {kind!r}; use stdio or http", err=True)
    raise typer.Exit(code=2)


@audit_app.command("verify")
def audit_verify(
    config_path: str = CONFIG_OPTION, policy_path: str = POLICY_OPTION
) -> None:
    """Recompute the hash chain and report any break."""
    config = _load(config_path, policy_path)
    wired = wiring.build(config)
    result = wired.services.audit.verify()
    typer.echo(str(result))
    if not result.ok:
        raise typer.Exit(code=1)


@audit_app.command("log")
def audit_log(
    limit: int = typer.Option(20, help="How many recent records to show."),
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
) -> None:
    """Print recent audit records."""
    config = _load(config_path, policy_path)
    wired = wiring.build(config)
    for entry in wired.services.audit.entries(limit=limit):
        typer.echo(
            f"{entry.sequence:>5}  {entry.at:%Y-%m-%d %H:%M:%S}  "
            f"{entry.event:<18} {entry.actor:<16} {entry.payload}"
        )


@approvals_app.command("list")
def approvals_list(
    status: str = typer.Option("pending", help="pending, approved, rejected or failed."),
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
) -> None:
    """List queued actions."""
    config = _load(config_path, policy_path)
    wired = wiring.build(config)
    items = wired.services.queue.list(status, config.company.name)
    if not items:
        typer.echo(f"No {status} actions.")
        return
    for item in items:
        flag = "" if (item.validation is None or item.validation.ok) else "  [VALIDATION FAILED]"
        typer.echo(f"{item.ticket}  {item.amount:>12}  {item.source:<10} {item.summary}{flag}")


@approvals_app.command("stats")
def approvals_stats(
    config_path: str = CONFIG_OPTION, policy_path: str = POLICY_OPTION
) -> None:
    """Per-action-type history, to inform a policy promotion decision."""
    config = _load(config_path, policy_path)
    wired = wiring.build(config)
    stats = wired.services.queue.stats(config.company.name)
    if not stats:
        typer.echo("No approval history yet.")
        return
    typer.echo(f"{'action type':<26}{'total':>7}{'appr':>7}{'rej':>6}{'rate':>8}  recommendation")
    for stat in stats:
        typer.echo(
            f"{stat.action_type:<26}{stat.total:>7}{stat.approved:>7}"
            f"{stat.rejected:>6}{stat.approval_rate:>8.0%}  {stat.recommendation}"
        )
    typer.echo(
        "\nPromotion is a human edit to policy.toml. Nothing here changes policy."
    )


@approvals_app.command("approve")
def approvals_approve(
    ticket: str,
    actor: str = typer.Option(..., "--actor", help="Who is approving."),
    reason: str = typer.Option("", "--reason"),
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
    fake: bool = FAKE_OPTION,
) -> None:
    """Approve a queued action and post it."""
    config = _load(config_path, policy_path)
    wired = _wire(config, fake)
    result = asyncio.run(wired.services.queue.approve(ticket, actor, reason))
    if result.ok:
        number = f" as {result.voucher_number}" if result.voucher_number else ""
        typer.echo(f"{ticket} posted{number}.")
        return
    typer.echo(f"{ticket} failed: {'; '.join(result.errors)}", err=True)
    raise typer.Exit(code=1)


@approvals_app.command("reject")
def approvals_reject(
    ticket: str,
    reason: str = typer.Option(..., "--reason", help="Why. Required, and logged."),
    actor: str = typer.Option(..., "--actor"),
    config_path: str = CONFIG_OPTION,
    policy_path: str = POLICY_OPTION,
) -> None:
    """Reject a queued action."""
    config = _load(config_path, policy_path)
    wired = wiring.build(config)
    wired.services.queue.reject(ticket, actor, reason)
    typer.echo(f"{ticket} rejected.")


@app.command("fake-tally")
def fake_tally(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(9000),
) -> None:
    """Serve the fake Tally over HTTP, for the demo and for development."""
    script = Path(__file__).resolve().parents[3] / "scripts" / "dev_fake_tally.py"
    typer.echo(f"Serving the fake Tally on http://{host}:{port}")
    import runpy

    sys.argv = [str(script), "--host", host, "--port", str(port)]
    runpy.run_path(str(script), run_name="__main__")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
