"""Configuration loading.

One rule that shapes everything here: secrets never come from the file. The TOML
carries hosts, ports, policy and allowlists; keys and tokens come from the
environment or the OS keyring. A config file gets emailed around, committed, and
backed up; an environment variable does not.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from tallyagent_agent.tiers import TierConfig
from tallyagent_channels.whatsapp.meta import WhatsAppConfig, normalise_number
from tallyagent_core.livemode import DEFAULT_WRITE_PREFIX, LiveMode
from tallyagent_core.models import Company, Period
from tallyagent_core.policy import Policy
from tallyagent_llm.router import ModelConfig
from tallyagent_tally.client import TallyConfig

#: The shipped default. Deliberately non-routable: a fresh install must not be
#: able to reach a real Tally until someone edits the file.
PLACEHOLDER_HOST = "0.0.0.0"


@dataclass(slots=True)
class DaemonConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    tray: bool = True


@dataclass(slots=True)
class McpConfig:
    enabled: bool = True
    transport: str = "stdio"
    read_only: bool = False


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    egress_log: str = "./egress.jsonl"


@dataclass(slots=True)
class Config:
    #: "fake" (default) or "live". Live mode turns on the write-scope guard and,
    #: on a student install, the Educational date rule.
    mode: str = "fake"
    live: LiveMode = field(default_factory=LiveMode)
    tally: TallyConfig = field(default_factory=TallyConfig)
    company: Company = field(default_factory=lambda: Company(name="", state_code="27"))
    model: ModelConfig = field(default_factory=ModelConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    tiers: TierConfig = field(default_factory=TierConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    policy: Policy = field(default_factory=Policy.default)
    db_path: str = "./tallyagent.db"

    @property
    def tally_is_placeholder(self) -> bool:
        return self.tally.host in (PLACEHOLDER_HOST, "", "0.0.0.0")

    @property
    def is_live(self) -> bool:
        return self.live.enabled


def _date(raw: Any) -> date | None:
    if isinstance(raw, date):
        return raw
    if not raw:
        return None
    return date.fromisoformat(str(raw))


def load(
    path: str | Path = "config/config.toml",
    policy_path: str | Path | None = None,
) -> Config:
    """Read config.toml. A missing file yields defaults, not an error - the
    tray app must still start so the user can be told what to fill in."""
    data: dict[str, Any] = {}
    file = Path(path)
    if file.exists():
        with file.open("rb") as fh:
            data = tomllib.load(fh)
    return from_dict(data, policy_path)


def from_dict(
    data: dict[str, Any], policy_path: str | Path | None = None
) -> Config:
    tally_raw = data.get("tally") or {}
    company_raw = data.get("company") or {}
    model_raw = data.get("model") or {}
    daemon_raw = data.get("daemon") or {}
    tiers_raw = data.get("tiers") or {}
    storage_raw = data.get("storage") or {}
    mcp_raw = data.get("mcp") or {}
    logging_raw = data.get("logging") or {}
    whatsapp_raw = (data.get("channels") or {}).get("whatsapp") or {}

    period = None
    if company_raw.get("fy_start") and company_raw.get("fy_end"):
        period = Period(
            start=_date(company_raw["fy_start"]),  # type: ignore[arg-type]
            end=_date(company_raw["fy_end"]),  # type: ignore[arg-type]
            locked_before=_date(company_raw.get("locked_before")),
        )

    company = Company(
        name=str(tally_raw.get("company") or company_raw.get("name") or ""),
        state_code=str(company_raw.get("state_code") or "27"),
        gstin=str(company_raw.get("gstin") or "") or None,
        period=period,
    )

    policy = Policy.default()
    if policy_path and Path(policy_path).exists():
        policy = Policy.load(policy_path)

    allowlist = {
        normalise_number(number): str(company_name)
        for number, company_name in (whatsapp_raw.get("allowlist") or {}).items()
    }

    mode = str(data.get("mode") or "fake").strip().lower()
    live_enabled = mode == "live"
    # EDU defaults ON in live mode: a student install is the common case, and
    # being wrong in that direction costs a warning, not a rejected voucher.
    edu = bool(tally_raw.get("edu", live_enabled))

    return Config(
        mode=mode,
        live=LiveMode(
            enabled=live_enabled,
            write_prefix=str(tally_raw.get("write_prefix") or DEFAULT_WRITE_PREFIX),
            edu=edu,
        ),
        tally=TallyConfig(
            host=str(tally_raw.get("host") or PLACEHOLDER_HOST),
            port=int(tally_raw.get("port") or 9000),
            company=company.name,
            username=str(tally_raw.get("username") or ""),
            # Password is a secret: env only.
            password=os.environ.get("TALLY_PASSWORD", ""),
            timeout_seconds=float(tally_raw.get("timeout_seconds") or 60),
            supports_voucher_alter=bool(
                tally_raw.get("supports_voucher_alter", False)
            ),
        ),
        company=company,
        model=ModelConfig(
            provider=str(model_raw.get("provider") or "deepseek"),
            model=str(model_raw.get("model") or "deepseek-flash"),
            base_url=str(model_raw.get("base_url") or ""),
            max_steps=int(model_raw.get("max_steps") or 12),
        ),
        daemon=DaemonConfig(
            host=str(daemon_raw.get("host") or "127.0.0.1"),
            port=int(daemon_raw.get("port") or 8787),
            tray=bool(daemon_raw.get("tray", True)),
        ),
        tiers=TierConfig(
            perception_enabled=bool(tiers_raw.get("perception_enabled", False)),
            perception_interval_seconds=int(
                tiers_raw.get("perception_interval_seconds") or 30
            ),
            fallback_enabled=bool(tiers_raw.get("fallback_enabled", False)),
            fallback_max_steps=int(tiers_raw.get("fallback_max_steps") or 10),
        ),
        whatsapp=WhatsAppConfig(
            enabled=bool(whatsapp_raw.get("enabled", False)),
            provider=str(whatsapp_raw.get("provider") or "meta"),
            verify_token=str(whatsapp_raw.get("verify_token") or ""),
            app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
            access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=str(whatsapp_raw.get("phone_number_id") or ""),
            allowlist=allowlist,
        ),
        mcp=McpConfig(
            enabled=bool(mcp_raw.get("enabled", True)),
            transport=str(mcp_raw.get("transport") or "stdio"),
            read_only=bool(mcp_raw.get("read_only", False)),
        ),
        logging=LoggingConfig(
            level=str(logging_raw.get("level") or "INFO"),
            egress_log=str(logging_raw.get("egress_log") or "./egress.jsonl"),
        ),
        policy=policy,
        db_path=str(storage_raw.get("db_path") or "./tallyagent.db"),
    )
