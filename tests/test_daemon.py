"""Stage 12: config loading, wiring, the app, the scheduler, the CLI, packaging."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_daemon.app import build_app
from tallyagent_daemon.cli import app as cli_app
from tallyagent_daemon.scheduler import Scheduler
from tallyagent_tally.fake_server import seeded_demo

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.fixture
def demo_config(tmp_path):
    config = config_mod.load(ROOT / "config" / "config.example.toml")
    config = config_mod.Config(
        tally=config.tally,
        company=config.company.model_copy(update={"name": "Demo Traders Pvt Ltd"}),
        model=config.model,
        daemon=config.daemon,
        tiers=config.tiers,
        whatsapp=config.whatsapp,
        mcp=config.mcp,
        logging=config_mod.LoggingConfig(egress_log=str(tmp_path / "egress.jsonl")),
        policy=config.policy,
        db_path=str(tmp_path / "tallyagent.db"),
    )
    config.model.provider = "mock"
    config.tally.company = "Demo Traders Pvt Ltd"
    return config


@pytest.fixture
def wired(demo_config):
    return wiring.build(demo_config, transport=seeded_demo("Demo Traders Pvt Ltd").transport)


# --- config -----------------------------------------------------------------


def test_the_example_config_parses():
    config = config_mod.load(ROOT / "config" / "config.example.toml")
    assert config.tally.port == 9000
    assert config.company.state_code == "27"
    assert config.model.provider == "deepseek"
    assert config.model.model == "deepseek-flash"
    assert config.daemon.host == "127.0.0.1"
    assert config.company.period.start.isoformat() == "2026-04-01"


def test_the_shipped_tally_host_is_a_non_routable_placeholder():
    config = config_mod.load(ROOT / "config" / "config.example.toml")
    assert config.tally.host == config_mod.PLACEHOLDER_HOST
    assert config.tally_is_placeholder


def test_tiers_are_off_by_default_in_the_example_config():
    config = config_mod.load(ROOT / "config" / "config.example.toml")
    assert not config.tiers.perception_enabled
    assert not config.tiers.fallback_enabled
    assert not config.whatsapp.enabled


def test_secrets_are_never_read_from_the_file(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[tally]\nhost = "127.0.0.1"\npassword = "hunter2"\n'
        '[channels.whatsapp]\napp_secret = "from-the-file"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("TALLY_PASSWORD", raising=False)
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    config = config_mod.load(path)
    assert config.tally.password == ""
    assert config.whatsapp.app_secret == ""

    monkeypatch.setenv("TALLY_PASSWORD", "from-the-env")
    assert config_mod.load(path).tally.password == "from-the-env"


def test_a_missing_config_file_yields_defaults_not_an_error(tmp_path):
    config = config_mod.load(tmp_path / "nope.toml")
    assert config.tally_is_placeholder
    assert config.policy.requires_approval("create_sales_voucher", 1)


def test_whatsapp_allowlist_numbers_are_normalised(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[channels.whatsapp]\nenabled = true\n'
        '[channels.whatsapp.allowlist]\n"+91 98765 43210" = "Demo Co"\n',
        encoding="utf-8",
    )
    config = config_mod.load(path)
    assert config.whatsapp.company_for("919876543210") == "Demo Co"


def test_policy_file_is_loaded_when_present():
    config = config_mod.load(
        ROOT / "config" / "config.example.toml",
        ROOT / "config" / "policy.example.toml",
    )
    assert config.policy.allow_validation_override
    assert not config.policy.requires_approval("create_receipt", 100)


# --- wiring -----------------------------------------------------------------


async def test_wiring_produces_one_working_path_end_to_end(wired, tmp_path):
    from datetime import date

    from tallyagent_tools import vouchers

    services = wired.services
    result = await vouchers.create_sales_voucher(
        services.tools,
        "Acme Industries",
        "10000.00",
        "18",
        date(2026, 6, 15),
        reference="WIRED-1",
    )
    assert result.ok
    ticket = result.data["ticket"]

    write = await services.queue.approve(ticket, "ca@firm.in")
    assert write.ok
    assert services.audit.verify().ok


async def test_the_egress_log_is_written_to_both_sinks(wired, demo_config):
    await wired.services.agent("t").run("cash position?")

    path = Path(demo_config.logging.egress_log)
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert record["provider"] == "mock"
    assert record["bytes_sent"] > 0
    assert "fields" in record

    from sqlmodel import Session, select

    from tallyagent_approvals.db import EgressRow

    with Session(wired.engine) as session:
        rows = list(session.exec(select(EgressRow)))
    assert rows and rows[0].provider == "mock"


def test_a_missing_api_key_falls_back_to_the_mock_loudly(demo_config, caplog):
    demo_config.model.provider = "deepseek"
    with caplog.at_level("WARNING"):
        wired = wiring.build(demo_config)
    assert wired.using_mock_model
    assert any("mock provider" in record.message for record in caplog.records)


def test_idempotency_is_persistent_not_in_memory(wired):
    from tallyagent_approvals.db import SqlIdempotencyStore

    assert isinstance(wired.backend.store, SqlIdempotencyStore)


# --- app --------------------------------------------------------------------


def test_the_app_serves_the_ui_and_health(wired):
    with TestClient(build_app(wired)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").json()["company"] == "Demo Traders Pvt Ltd"


def test_whatsapp_is_not_mounted_when_disabled(wired):
    with TestClient(build_app(wired)) as client:
        assert client.post("/whatsapp/webhook", json={}).status_code == 404


def test_enabling_whatsapp_without_a_secret_refuses_to_start(wired):
    wired.config.whatsapp.enabled = True
    wired.config.whatsapp.app_secret = ""
    with pytest.raises(RuntimeError, match="WHATSAPP_APP_SECRET"):
        build_app(wired)


def test_enabling_whatsapp_with_a_secret_mounts_the_webhook(wired):
    wired.config.whatsapp.enabled = True
    wired.config.whatsapp.app_secret = "secret"
    with TestClient(build_app(wired)) as client:
        # Mounted, and rejecting an unsigned request rather than 404ing.
        assert client.post("/whatsapp/webhook", json={}).status_code == 401


# --- scheduler --------------------------------------------------------------


def test_job_scheduling_windows(wired):
    scheduler = Scheduler(wired)
    assert not scheduler.due_bank_reco(datetime(2026, 6, 15, 9, tzinfo=UTC))
    assert scheduler.due_bank_reco(datetime(2026, 6, 15, 22, 30, tzinfo=UTC))
    assert not scheduler.due_gstr2b(datetime(2026, 6, 10, tzinfo=UTC))
    assert scheduler.due_gstr2b(datetime(2026, 6, 14, tzinfo=UTC))


async def test_scheduled_jobs_report_and_never_post(wired, fake_tally):
    scheduler = Scheduler(wired)
    now = datetime(2026, 6, 15, 22, 30, tzinfo=UTC)

    runs = await scheduler.tick(now)
    assert {run.name for run in runs} == {"bank_reco", "gstr2b"}
    assert all(run.ok for run in runs)

    # Both jobs are recorded in the audit log, and neither wrote a voucher.
    events = [e for e in wired.services.audit.entries() if e.event == "scheduled_job"]
    assert {e.payload["job"] for e in events} == {"bank_reco", "gstr2b"}
    assert fake_tally.vouchers == []

    # And they do not run twice in the same window.
    assert await scheduler.tick(now) == []


async def test_a_failing_job_is_recorded_not_raised(wired, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError("Tally went away")

    monkeypatch.setattr("tallyagent_tools.reports.day_book", explode)
    run = await Scheduler(wired).run_bank_reco(datetime(2026, 6, 15, 22, tzinfo=UTC))
    assert not run.ok
    assert "Tally went away" in run.summary


# --- CLI --------------------------------------------------------------------


def test_probe_refuses_the_placeholder_host_with_advice():
    result = runner.invoke(
        cli_app, ["probe", "--config", str(ROOT / "config" / "config.example.toml")]
    )
    assert result.exit_code == 2
    assert "placeholder" in result.output
    assert "--fake-tally" in result.output


def test_probe_against_the_fake_tally_lists_the_demo_company(tmp_path):
    result = runner.invoke(
        cli_app,
        [
            "probe",
            "--fake-tally",
            "--config",
            str(ROOT / "config" / "config.example.toml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "fake-tally" in result.output
    assert "Demo Traders Pvt Ltd" in result.output
    assert "6.0.3" in result.output


def test_audit_verify_reports_an_intact_chain(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[storage]\ndb_path = "{(tmp_path / "a.db").as_posix()}"\n', encoding="utf-8"
    )
    result = runner.invoke(cli_app, ["audit", "verify", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "Audit chain intact" in result.output


def test_approvals_stats_says_promotion_is_a_human_decision(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[storage]\ndb_path = "{(tmp_path / "b.db").as_posix()}"\n', encoding="utf-8"
    )
    result = runner.invoke(cli_app, ["approvals", "stats", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "No approval history yet." in result.output


def test_cli_help_lists_every_command():
    output = runner.invoke(cli_app, ["--help"]).output
    for command in ("probe", "chat", "serve", "mcp", "audit", "approvals", "fake-tally"):
        assert command in output


# --- packaging --------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "Dockerfile",
        "tallyagent.spec",
        "scripts/build_windows.ps1",
        "scripts/dev_fake_tally.py",
        "scripts/seed_demo_company.py",
        "config/config.example.toml",
        "config/policy.example.toml",
    ],
)
def test_packaging_artefacts_exist(path):
    assert (ROOT / path).exists(), f"{path} is missing"


def test_the_dockerfile_does_not_run_as_root():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER tallyagent" in text
    assert "HEALTHCHECK" in text


def test_the_spec_ships_the_templates():
    text = (ROOT / "tallyagent.spec").read_text(encoding="utf-8")
    assert "web/templates" in text


def test_the_windows_build_runs_the_tests_before_building():
    text = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    assert "uv run pytest" in text
    assert text.index("uv run pytest") < text.index("pyinstaller")


async def test_the_demo_seed_script_produces_a_balanced_book():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import seed_demo_company

    for voucher in seed_demo_company.VOUCHERS:
        assert voucher.is_balanced, f"{voucher.reference} does not balance"
