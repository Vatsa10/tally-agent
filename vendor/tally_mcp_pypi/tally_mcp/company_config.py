"""Multi-company configuration, discovery, and routing.

Handles onboarding (discover companies from running Tally), persistent
configuration (companies.json), heartbeat tracking, and date-based routing.
"""

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field


# ── Configuration models ──────────────────────────────────────────


class TallyCompanyEntry(BaseModel):
    """A single Tally company file with its metadata."""

    company: str = Field(description="Exact Tally company name")
    from_date: str = Field("", description="Period start YYYYMMDD (e.g. 20200401)")
    to_date: str = Field("", description="Period end YYYYMMDD (e.g. 20220331)")


class BusinessConfig(BaseModel):
    """A logical business that may span multiple Tally company files."""

    name: str = Field(description="Friendly business name (used by LLM)")
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative names the user might use",
    )
    tally_companies: list[TallyCompanyEntry] = Field(
        default_factory=list,
        description="Tally company files, ordered by date range",
    )
    default_company: str = Field(
        "",
        description="Which Tally company to use when no date is specified",
    )


class CompaniesConfig(BaseModel):
    """Root configuration for all businesses."""

    businesses: list[BusinessConfig] = Field(default_factory=list)


# ── Heartbeat status ─────────────────────────────────────────────


class CompanyStatus(BaseModel):
    """Last-known status for a Tally company."""

    company: str
    last_seen: float = 0  # Unix timestamp
    last_seen_human: str = ""
    is_open: bool = False
    transport: str = ""  # "json" or "xml"


class StatusLog(BaseModel):
    """Heartbeat status for all known companies."""

    companies: dict[str, CompanyStatus] = Field(default_factory=dict)
    last_updated: float = 0
    last_updated_human: str = ""


# ── File paths ────────────────────────────────────────────────────


def _config_dir() -> Path:
    """Config directory — platform user config dir.

    Can be overridden via TALLY_MCP_CONFIG_DIR env var.
    Windows: %APPDATA%/tally-mcp/
    Linux/macOS: $XDG_CONFIG_HOME/tally-mcp/ or ~/.config/tally-mcp/
    """
    override = os.environ.get("TALLY_MCP_CONFIG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "tally-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return _config_dir() / "companies.json"


def status_path() -> Path:
    return _config_dir() / "companies_status.json"


# ── Load / Save ───────────────────────────────────────────────────


def load_config() -> CompaniesConfig:
    """Load companies.json if it exists, otherwise return empty config."""
    p = config_path()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        return CompaniesConfig(**data)
    return CompaniesConfig()


def save_config(config: CompaniesConfig) -> str:
    """Write companies.json. Returns the path written to."""
    p = config_path()
    p.write_text(
        json.dumps(config.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(p)


def load_status() -> StatusLog:
    """Load heartbeat status log."""
    p = status_path()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        return StatusLog(**data)
    return StatusLog()


def save_status(status: StatusLog) -> None:
    """Write heartbeat status log."""
    p = status_path()
    now = time.time()
    status.last_updated = now
    status.last_updated_human = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    p.write_text(
        json.dumps(status.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def update_company_status(
    company: str,
    is_open: bool,
    transport: str = "",
) -> None:
    """Update heartbeat status for a single company."""
    log = load_status()
    now = time.time()
    entry = log.companies.get(company, CompanyStatus(company=company))
    entry.is_open = is_open
    if is_open:
        entry.last_seen = now
        entry.last_seen_human = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(now)
        )
    if transport:
        entry.transport = transport
    log.companies[company] = entry
    save_status(log)


# ── Company Router ────────────────────────────────────────────────


class ResolvedCompany(BaseModel):
    """Result of routing a query to a specific Tally company."""

    company: str
    business_name: str = ""


class CompanyRouter:
    """Resolves business name + date range to a specific Tally company entry."""

    def __init__(self, config: CompaniesConfig, fallback_company: str = ""):
        self.config = config
        self.fallback_company = fallback_company

        # Build lookup indexes
        self._by_business: dict[str, BusinessConfig] = {}
        self._by_alias: dict[str, BusinessConfig] = {}
        self._by_company: dict[str, tuple[BusinessConfig, TallyCompanyEntry]] = {}

        for biz in config.businesses:
            key = biz.name.lower()
            self._by_business[key] = biz
            for alias in biz.aliases:
                self._by_alias[alias.lower()] = biz
            for entry in biz.tally_companies:
                self._by_company[entry.company.lower()] = (biz, entry)

    def resolve(
        self,
        company: str = "",
        business: str = "",
        from_date: str = "",
        to_date: str = "",
    ) -> ResolvedCompany:
        """Resolve to a specific Tally company.

        Priority:
        1. Exact company name match
        2. Business name + date range
        3. Business name + default company
        4. Fallback from .env
        """
        # 1. Exact company name
        if company:
            key = company.lower()
            if key in self._by_company:
                biz, entry = self._by_company[key]
                return ResolvedCompany(
                    company=entry.company,
                    business_name=biz.name,
                )
            # Not in config — use as-is
            return ResolvedCompany(company=company)

        # 2/3. Business name lookup
        biz = None
        if business:
            bkey = business.lower()
            biz = self._by_business.get(bkey) or self._by_alias.get(bkey)

        if biz:
            # Try date-based routing
            if from_date or to_date:
                query_date = from_date or to_date
                for entry in biz.tally_companies:
                    if entry.from_date and entry.to_date:
                        if entry.from_date <= query_date <= entry.to_date:
                            return ResolvedCompany(
                                company=entry.company,
                                business_name=biz.name,
                            )

            # Fall back to default company for this business
            if biz.default_company:
                return ResolvedCompany(
                    company=biz.default_company,
                    business_name=biz.name,
                )

        # 4. Fallback
        return ResolvedCompany(company=self.fallback_company)
