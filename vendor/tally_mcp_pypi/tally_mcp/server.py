import contextlib
import csv
import io
import json
import logging
import os
import sys
import time
import xml.sax.saxutils as saxutils
from logging.handlers import RotatingFileHandler

from fastmcp import FastMCP, Context

from tally_mcp.auth import (
    AccessDeniedError,
    check_admin,
    get_auth_provider,
    get_current_user,
)
from tally_mcp.company_config import (
    BusinessConfig,
    CompaniesConfig,
    CompanyRouter,
    TallyCompanyEntry,
    _config_dir,
    config_path,
    load_config,
    load_status,
    save_config,
    update_company_status,
)
from tally_mcp.config import settings
from tally_mcp.confirm import summarize_import_xml
from tally_mcp.models import (
    GroupCreate,
    InventoryEntry,
    LedgerCreate,
    StockItemCreate,
    VoucherCreate,
    VoucherEntry,
)
from tally_mcp.tally_client import TallyClient
from tally_mcp.voucher_builder import (
    build_group_xml,
    build_ledger_xml,
    build_stock_item_xml,
    build_voucher_xml,
)

# ── Logging ──────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configure file logging in the config directory."""
    from tally_mcp.company_config import _config_dir
    log_dir = _config_dir()
    log_file = log_dir / "tally-mcp.log"
    handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

_setup_logging()
log = logging.getLogger("tally_mcp")

mcp = FastMCP(
    name="Tally MCP Server",
    instructions="""MCP server for TallyPrime accounting software (Indian accounting).

FIRST TIME SETUP: If the user hasn't configured companies yet, call discover_companies()
to scan Tally for open companies. Then use save_company_config() to save the setup.

RETURNING USERS: Call list_businesses() to see configured companies and their periods.
Use the 'business' parameter for auto-routing by date range.

DATES: Indian financial year runs April-March. Use YYYYMMDD format.
FY 2025-26 = from_date=20250401, to_date=20260331.

COMMON PATTERNS:
- Customer outstanding with ageing: get_receivables
- Vendor outstanding: get_payables
- Revenue/profit for a period: get_profit_and_loss with date range
- Monthly trends: call get_profit_and_loss once per month
- Monthly sales × customer grid: get_sales_by_party (ONE call, not per-customer)
- Monthly purchases × vendor grid: get_sales_by_party with voucher_type="Purchase"
- Specific party transactions: get_ledger_vouchers with ledger name
- Cash/bank balance: get_trial_balance, look at Bank Accounts / Cash-in-Hand groups

AMOUNTS: In reports, negative = credit (income/liability), positive = debit (expense/asset).
""",
    auth=get_auth_provider(),
)

_client: TallyClient | None = None

_READ_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": True,
}

_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}


def get_client() -> TallyClient:
    global _client
    if _client is None:
        _client = TallyClient(
            host=settings.tally_host,
            port=settings.tally_port,
            default_company=settings.tally_default_company,
            username=settings.tally_username,
            password=settings.tally_password,
        )
    return _client


def _report_to_str(result: str | dict) -> str:
    """Normalize report output to string (XML or JSON text)."""
    if isinstance(result, dict):
        return json.dumps(result, indent=2, ensure_ascii=False)
    return result


_audit_log = logging.getLogger("tally_mcp.audit")


def _check_role(company: str, require_write: bool = False, tool_name: str = "") -> None:
    """Unified access + role enforcement with audit logging.

    No-op when no auth is configured (stdio / open access).
    Logs all access checks (success and denial) when tool_name is provided.

    Raises AccessDeniedError when:
    - read user calls a write tool (require_write=True)
    - scoped user doesn't specify a company (returns company list)
    - company not in user's scope
    """
    user_info = get_current_user()
    if user_info is None:
        return  # No auth (stdio) — open access

    name, role, companies = user_info

    def _deny(msg: str) -> AccessDeniedError:
        if tool_name:
            _audit_log.info(
                "DENIED user=%s role=%s tool=%s company=%r",
                name, role, tool_name, company or "",
            )
        return AccessDeniedError(msg)

    # Role enforcement for write operations
    if require_write and role == "read":
        raise _deny(
            f"Access denied: role 'read' cannot perform write operations. "
            f"Your accessible companies: {companies}"
        )

    # Wildcard admin — always allowed past company check
    if "*" in companies:
        if tool_name:
            _audit_log.info(
                "ACCESS user=%s role=%s tool=%s company=%r",
                name, role, tool_name, company or "",
            )
        return

    # Empty company with scoped user — must choose
    if not company:
        raise _deny(
            f"Please specify a company. Your accessible companies: {companies}. "
            f"Call the tool again with one of these as the 'company' parameter."
        )

    # Company scope check
    if company not in companies:
        raise _deny(
            f"Access denied: no access to company '{company}'. "
            f"Your accessible companies: {companies}"
        )

    # Access granted — log it
    if tool_name:
        _audit_log.info(
            "ACCESS user=%s role=%s tool=%s company=%r",
            name, role, tool_name, company or "",
        )


@contextlib.contextmanager
def _audit(tool_name: str, company: str):
    """Time and log a tool call with user identity."""
    user_info = get_current_user()
    user_name = user_info[0] if user_info else "anonymous"
    role = user_info[1] if user_info else "none"
    t0 = time.monotonic()
    status = "ok"
    try:
        yield
    except AccessDeniedError:
        status = "denied"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _audit_log.info(
            "TOOL_CALL user=%s role=%s tool=%s company=%r status=%s duration_ms=%d",
            user_name, role, tool_name, company or "", status, duration_ms,
        )




async def _confirm_and_send(
    ctx: Context,
    data_xml: str,
    request_type: str,
    company: str,
    tool_name: str = "write_tool",
) -> dict:
    """Build import XML, show a confirmation summary, elicit user approval,
    then send to Tally only if confirmed."""
    with _audit(tool_name, company):
        _check_role(company, require_write=True, tool_name=tool_name)
        client = get_client()
        xml = client.build_import_xml(data_xml, request_type, company=company)
        summary = summarize_import_xml(xml)

        result = await ctx.elicit(
            f"Review the following changes before sending to Tally:\n\n{summary}\n\nProceed?",
            response_type=None,
        )
        if result.action != "accept":
            return {"status": "cancelled", "message": "Operation cancelled by user."}

        return await client.send_import(xml)


# --- Utility tools ---


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def health_check() -> dict:
    """Check if Tally is running and return list of open companies."""
    from tally_mcp import __version__
    result = await get_client().health_check()
    result["server_version"] = __version__
    return result


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def heartbeat(company: str = "") -> dict:
    """Quick connectivity check. Verifies Tally is reachable and the specified company is open.

    Returns {ok: bool, company: str, message: str}. Call this before write operations
    to avoid sending data to a crashed or disconnected Tally instance.
    """
    from tally_mcp import __version__
    _check_role(company, tool_name="heartbeat")
    result = await get_client().heartbeat(company=company or None)
    result["server_version"] = __version__
    # Update heartbeat log
    if company:
        update_company_status(
            company=company or settings.tally_default_company,
            is_open=result.get("ok", False),
            transport=result.get("transport", ""),
        )
    return result


# --- Onboarding & Company Management tools ---


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def discover_companies() -> dict:
    """Scan the running TallyPrime instance and discover all open companies.

    Call this when setting up for the first time, or when the user has opened
    new companies in Tally and wants to update the configuration.

    Returns:
    - companies: list of open company names
    - already_configured: list of company names already in companies.json
    - unconfigured: list of company names not yet configured
    - transport: "json" or "xml" (detected Tally version)
    - config_exists: whether companies.json already exists

    If Tally is not running or unreachable, returns an error message explaining
    what the user needs to do (start TallyPrime, enable server mode, etc.).

    Requires admin access when auth is configured.
    """
    check_admin()
    client = get_client()
    try:
        hb = await client.heartbeat()
    except Exception:
        return {
            "error": "Cannot reach TallyPrime. Please ensure:\n"
                     "1. TallyPrime is running\n"
                     "2. Server mode is enabled (F1 > Settings > Connectivity > "
                     "Client/Server Configuration > TallyPrime acts as Server)\n"
                     f"3. Port {settings.tally_port} is correct "
                     f"(current: http://{settings.tally_host}:{settings.tally_port})",
        }

    if not hb.get("ok"):
        return {"error": hb.get("message", "Tally unreachable")}

    # Get company list
    try:
        health = await client.health_check()
    except Exception as exc:
        return {"error": f"Connected but failed to list companies: {exc}"}

    companies = health["companies"]
    transport = client.transport

    # Update heartbeat log for all discovered companies
    for co in companies:
        update_company_status(co, is_open=True, transport=transport)

    # Check what's already configured
    existing_config = load_config()
    configured_names = set()
    for biz in existing_config.businesses:
        for entry in biz.tally_companies:
            configured_names.add(entry.company)

    unconfigured = [c for c in companies if c not in configured_names]
    already = [c for c in companies if c in configured_names]

    return {
        "companies": companies,
        "already_configured": already,
        "unconfigured": unconfigured,
        "transport": transport,
        "config_exists": config_path().exists(),
        "config_path": str(config_path()),
    }


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def save_company_config(
    ctx: Context,
    businesses_json: str,
) -> dict:
    """Save the multi-company configuration after discussing with the user.

    Call this after discover_companies() and after asking the user about:
    1. Which companies belong to the same business (group them)
    2. What date range each company covers (from_date, to_date in YYYYMMDD)
    3. A friendly business name and any aliases
    4. Which company is the default (usually the most recent one)

    Input: JSON string with this structure:
    {
      "businesses": [
        {
          "name": "My Business",
          "aliases": ["MB", "mybiz"],
          "tally_companies": [
            {
              "company": "My Business FY2022-24",
              "from_date": "20220401",
              "to_date": "20240331"
            },
            {
              "company": "My Business FY2024-26",
              "from_date": "20240401",
              "to_date": "20260331"
            }
          ],
          "default_company": "My Business FY2024-26"
        }
      ]
    }

    This OVERWRITES any existing companies.json. The user must confirm.

    Requires admin access when auth is configured.
    """
    check_admin()
    try:
        data = json.loads(businesses_json)
        config = CompaniesConfig(**data)
    except Exception as exc:
        return {"error": f"Invalid configuration: {exc}"}

    # Validate: each business must have at least one company
    for biz in config.businesses:
        if not biz.tally_companies:
            return {"error": f"Business '{biz.name}' has no Tally companies configured."}
        if not biz.default_company:
            # Auto-set to last company (most recent)
            biz.default_company = biz.tally_companies[-1].company

    # Show summary and confirm
    lines = ["Company configuration to save:\n"]
    for biz in config.businesses:
        lines.append(f"Business: {biz.name}")
        if biz.aliases:
            lines.append(f"  Aliases: {', '.join(biz.aliases)}")
        for entry in biz.tally_companies:
            period = ""
            if entry.from_date and entry.to_date:
                period = f" ({entry.from_date} - {entry.to_date})"
            default = " (DEFAULT)" if entry.company == biz.default_company else ""
            lines.append(f"  - {entry.company}{period}{default}")
        lines.append("")

    summary = "\n".join(lines)
    result = await ctx.elicit(
        f"{summary}\nSave this configuration?",
        response_type=None,
    )
    if result.action != "accept":
        return {"status": "cancelled", "message": "Configuration not saved."}

    path = save_config(config)
    return {
        "status": "saved",
        "path": path,
        "businesses": len(config.businesses),
        "total_companies": sum(len(b.tally_companies) for b in config.businesses),
    }


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def list_businesses() -> dict:
    """List all configured businesses and their Tally company files.

    Call this at the start of a conversation to understand what companies
    are available and which date periods they cover. Use the business 'name'
    in subsequent queries for automatic date-based routing.

    Returns configured businesses with date ranges and last-known status.
    If no configuration exists, suggests calling discover_companies() first.
    """
    config = load_config()
    if not config.businesses:
        has_default = bool(settings.tally_default_company)
        return {
            "configured": False,
            "message": "No companies configured yet. "
                       + ("Call discover_companies() to scan Tally and set up."
                          if not has_default
                          else f"Using default company from .env: '{settings.tally_default_company}'. "
                               "Call discover_companies() to set up multi-company support."),
            "default_company": settings.tally_default_company,
        }

    # Load heartbeat status
    status_log = load_status()

    businesses = []
    for biz in config.businesses:
        companies = []
        for entry in biz.tally_companies:
            co_status = status_log.companies.get(entry.company)
            companies.append({
                "company": entry.company,
                "from_date": entry.from_date,
                "to_date": entry.to_date,
                "is_default": entry.company == biz.default_company,
                "last_seen": co_status.last_seen_human if co_status else "never",
                "is_open": co_status.is_open if co_status else None,
            })
        businesses.append({
            "name": biz.name,
            "aliases": biz.aliases,
            "companies": companies,
        })

    return {
        "configured": True,
        "businesses": businesses,
    }


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def check_all_companies() -> dict:
    """Heartbeat check on ALL configured companies. Updates the status log.

    Use this to verify which companies are currently open in Tally.
    Useful after Tally restart or when switching between company files.
    """
    client = get_client()

    # Get list of actually open companies
    try:
        health = await client.health_check()
    except Exception as exc:
        return {"error": f"Cannot reach Tally: {exc}"}

    open_companies = set(health["companies"])
    transport = client.transport

    # Check all configured companies against what's open
    config = load_config()
    results = []

    all_configured = set()
    for biz in config.businesses:
        for entry in biz.tally_companies:
            all_configured.add(entry.company)

    # Also include the .env default if set
    if settings.tally_default_company:
        all_configured.add(settings.tally_default_company)

    for co in all_configured | open_companies:
        is_open = co in open_companies
        update_company_status(co, is_open=is_open, transport=transport if is_open else "")
        status = "open" if is_open else "closed"
        configured = co in all_configured
        results.append({
            "company": co,
            "status": status,
            "configured": configured,
        })

    return {
        "transport": transport,
        "results": sorted(results, key=lambda r: (not r["configured"], r["company"])),
        "open_count": len(open_companies),
        "configured_count": len(all_configured),
    }


# --- Company & Master tools ---


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def list_companies() -> list[dict]:
    """List all companies currently open in Tally."""
    return await get_client().list_companies()


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def list_ledgers(
    company: str = "",
    group: str = "",
) -> list[dict]:
    """List all ledgers. Optionally filter by parent group name."""
    _check_role(company, tool_name="list_ledgers")
    client = get_client()
    if group:
        tdl = f'''
        <COLLECTION NAME="FilteredLedgers" ISMODIFY="No">
            <TYPE>Ledger</TYPE>
            <FILTER>GroupFilter</FILTER>
            <FETCH>Name, Parent, OpeningBalance, ClosingBalance</FETCH>
        </COLLECTION>
        <SYSTEM TYPE="Formulae" NAME="GroupFilter">$Parent = "{saxutils.escape(group, {'"': '&quot;'})}"</SYSTEM>
        '''
        return await client.export_collection(
            "FilteredLedgers", "LEDGER", company=company, tdl=tdl
        )
    return await client.export_collection("List of Ledgers", "LEDGER", company=company)


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_ledger(
    ledger_name: str,
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get detailed voucher report for a specific ledger."""
    _check_role(company, tool_name="get_ledger")
    extra = {"LEDGERNAME": ledger_name}
    return _report_to_str(await get_client().export_report(
        "Ledger Vouchers",
        company=company,
        from_date=from_date,
        to_date=to_date,
        extra_vars=extra,
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def list_groups(company: str = "") -> list[dict]:
    """List all account groups."""
    _check_role(company, tool_name="list_groups")
    return await get_client().export_collection("List of Groups", "GROUP", company=company)


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def list_stock_items(company: str = "") -> list[dict]:
    """List all stock items with their details."""
    _check_role(company, tool_name="list_stock_items")
    return await get_client().export_collection(
        "List of Stock Items", "STOCKITEM", company=company
    )


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_stock_item(
    stock_item_name: str,
    company: str = "",
) -> str:
    """Get detailed monthly movement report for a specific stock item."""
    _check_role(company, tool_name="get_stock_item")
    extra = {"STOCKITEMNAME": stock_item_name}
    return _report_to_str(await get_client().export_report("Stock Item", company=company, extra_vars=extra))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def list_voucher_types(company: str = "") -> list[dict]:
    """List all voucher types defined in Tally."""
    _check_role(company, tool_name="list_voucher_types")
    return await get_client().export_collection(
        "List of Voucher Types", "VOUCHERTYPE", company=company
    )


# --- Report tools ---


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_trial_balance(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get the Trial Balance report. Returns group-wise balances."""
    _check_role(company, tool_name="get_trial_balance")
    extra = {"EXPLODEFLAG": "Yes"}
    return _report_to_str(await get_client().export_report(
        "Trial Balance",
        company=company,
        from_date=from_date,
        to_date=to_date,
        extra_vars=extra,
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_balance_sheet(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get the Balance Sheet report."""
    _check_role(company, tool_name="get_balance_sheet")
    return _report_to_str(await get_client().export_report(
        "Balance Sheet", company=company, from_date=from_date, to_date=to_date
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_profit_and_loss(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get the Profit & Loss statement."""
    _check_role(company, tool_name="get_profit_and_loss")
    return _report_to_str(await get_client().export_report(
        "Profit and Loss A/c", company=company, from_date=from_date, to_date=to_date
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_day_book(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
    voucher_type: str = "",
) -> str:
    """Get the Day Book (list of all vouchers). Optionally filter by voucher type."""
    _check_role(company, tool_name="get_day_book")
    extra = {}
    if voucher_type:
        extra["VOUCHERTYPENAME"] = voucher_type
    return _report_to_str(await get_client().export_report(
        "Day Book", company=company, from_date=from_date, to_date=to_date, extra_vars=extra
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_stock_summary(company: str = "") -> str:
    """Get the Stock Summary report showing all stock items with quantities and values."""
    _check_role(company, tool_name="get_stock_summary")
    return _report_to_str(await get_client().export_report("Stock Summary", company=company))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_sales_register(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get the Sales Register report."""
    _check_role(company, tool_name="get_sales_register")
    return _report_to_str(await get_client().export_report(
        "Sales Register", company=company, from_date=from_date, to_date=to_date
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_sales_by_party(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
    voucher_type: str = "Sales",
    group_by: str = "monthly",
) -> str:
    """Get sales (or purchases) broken down by party (customer/vendor) and month.

    Returns a compact JSON grid: {party_name: {month: amount, ...}, ...}
    Much more efficient than calling get_ledger_vouchers per customer.

    Args:
        company: Company name (uses default if empty).
        from_date: Start date YYYYMMDD (e.g. 20250401).
        to_date: End date YYYYMMDD (e.g. 20260331).
        voucher_type: Voucher type to filter — "Sales", "Purchase", "Journal", etc.
        group_by: "monthly" (default) groups amounts by calendar month,
                  "total" returns one total per party.
    """
    _check_role(company, tool_name="get_sales_by_party")
    client = get_client()
    resolved_company = client._company(company)

    # Use inline TDL to export just the fields we need from vouchers
    tdl = f"""
        <COLLECTION NAME="VchByParty" ISINITIALIZE="Yes">
            <TYPE>Voucher</TYPE>
            <FILTER>VTypeFilter</FILTER>
            <NATIVEMETHOD>Date</NATIVEMETHOD>
            <NATIVEMETHOD>PartyLedgerName</NATIVEMETHOD>
            <NATIVEMETHOD>Amount</NATIVEMETHOD>
        </COLLECTION>
        <SYSTEM TYPE="Formulae" NAME="VTypeFilter">
            $VoucherTypeName = "{saxutils.escape(voucher_type, {'"': '&quot;'})}"
        </SYSTEM>
    """

    vouchers = await client.export_collection(
        collection_name="VchByParty",
        element_tag="VOUCHER",
        company=resolved_company,
        from_date=from_date,
        to_date=to_date,
        tdl=tdl,
    )

    if not vouchers:
        return json.dumps({"message": f"No {voucher_type} vouchers found in the period.", "grid": {}})

    # Build party × month grid
    from collections import defaultdict
    grid: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    party_totals: dict[str, float] = defaultdict(float)

    for v in vouchers:
        party = v.get("PARTYLEDGERNAME", "Unknown")
        amount_str = v.get("AMOUNT", "0")
        try:
            # Tally amounts may have commas or currency symbols
            amount = float(str(amount_str).replace(",", "").strip())
        except (ValueError, TypeError):
            continue

        # Amounts are negative for credit (sales income) — show as positive
        display_amount = abs(amount)

        if group_by == "monthly":
            date_str = v.get("DATE", "")
            # DATE comes as YYYYMMDD from collection export
            if len(date_str) >= 6:
                try:
                    year = date_str[:4]
                    month = int(date_str[4:6])
                    month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    month_key = f"{month_names[month]}-{year[2:]}"
                except (ValueError, IndexError):
                    month_key = "Unknown"
            else:
                month_key = "Unknown"
            grid[party][month_key] += display_amount

        party_totals[party] += display_amount

    # Format output
    if group_by == "monthly":
        result = {
            "grid": {party: dict(months) for party, months in sorted(grid.items())},
            "totals_by_party": dict(sorted(party_totals.items())),
            "grand_total": sum(party_totals.values()),
            "voucher_count": len(vouchers),
        }
    else:
        result = {
            "totals_by_party": dict(sorted(party_totals.items())),
            "grand_total": sum(party_totals.values()),
            "voucher_count": len(vouchers),
        }

    return json.dumps(result, indent=2)


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_purchase_register(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get the Purchase Register report."""
    _check_role(company, tool_name="get_purchase_register")
    return _report_to_str(await get_client().export_report(
        "Purchase Register", company=company, from_date=from_date, to_date=to_date
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_receivables(company: str = "") -> str:
    """Get outstanding receivables / bills receivable report."""
    _check_role(company, tool_name="get_receivables")
    return _report_to_str(await get_client().export_report("Bills Receivable", company=company))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_payables(company: str = "") -> str:
    """Get outstanding payables / bills payable report."""
    _check_role(company, tool_name="get_payables")
    return _report_to_str(await get_client().export_report("Bills Payable", company=company))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_bank_book(
    ledger_name: str,
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get the Cash/Bank book for a specific ledger (e.g., 'Cash', 'HDFC Bank')."""
    _check_role(company, tool_name="get_bank_book")
    extra = {"LEDGERNAME": ledger_name}
    return _report_to_str(await get_client().export_report(
        "Ledger Vouchers",
        company=company,
        from_date=from_date,
        to_date=to_date,
        extra_vars=extra,
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_ledger_vouchers(
    ledger_name: str,
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get all vouchers for a specific ledger within a date range."""
    _check_role(company, tool_name="get_ledger_vouchers")
    extra = {"LEDGERNAME": ledger_name}
    return _report_to_str(await get_client().export_report(
        "Ledger Vouchers",
        company=company,
        from_date=from_date,
        to_date=to_date,
        extra_vars=extra,
    ))


@mcp.tool(annotations=_READ_ANNOTATIONS)
async def get_group_summary(
    group_name: str,
    company: str = "",
    from_date: str = "",
    to_date: str = "",
) -> str:
    """Get summary for a specific account group showing ledgers under it."""
    _check_role(company, tool_name="get_group_summary")
    extra = {"GROUPNAME": group_name}
    return _report_to_str(await get_client().export_report(
        "Group Summary",
        company=company,
        from_date=from_date,
        to_date=to_date,
        extra_vars=extra,
    ))


# --- Write tools: Masters ---


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def create_ledger(
    ctx: Context,
    name: str,
    parent: str,
    company: str,
    opening_balance: float = 0.0,
    address: str = "",
    gst_number: str = "",
) -> dict:
    """Create a new ledger in Tally. Parent must be an existing group like 'Sundry Debtors', 'Sales Accounts', etc.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    ledger = LedgerCreate(
        name=name,
        parent=parent,
        opening_balance=opening_balance,
        address=address,
        gst_number=gst_number,
    )
    xml = build_ledger_xml(ledger)
    return await _confirm_and_send(ctx, xml, "All Masters", company, tool_name="create_ledger")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def alter_ledger(
    ctx: Context,
    name: str,
    company: str,
    parent: str = "",
    opening_balance: float = 0.0,
    address: str = "",
    gst_number: str = "",
) -> dict:
    """Alter/modify an existing ledger in Tally.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    ledger = LedgerCreate(
        name=name,
        parent=parent or "",
        opening_balance=opening_balance,
        address=address,
        gst_number=gst_number,
    )
    xml = build_ledger_xml(ledger, action="Alter")
    return await _confirm_and_send(ctx, xml, "All Masters", company, tool_name="alter_ledger")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def create_group(
    ctx: Context,
    name: str,
    parent: str,
    company: str,
) -> dict:
    """Create a new account group under a parent group.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    group = GroupCreate(name=name, parent=parent)
    xml = build_group_xml(group)
    return await _confirm_and_send(ctx, xml, "All Masters", company, tool_name="create_group")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def create_stock_item(
    ctx: Context,
    name: str,
    company: str,
    parent: str = "",
    unit: str = "Nos",
    hsn_code: str = "",
    gst_rate: float = 0.0,
) -> dict:
    """Create a new stock item in Tally.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    item = StockItemCreate(name=name, parent=parent, unit=unit, hsn_code=hsn_code, gst_rate=gst_rate)
    xml = build_stock_item_xml(item)
    return await _confirm_and_send(ctx, xml, "All Masters", company, tool_name="create_stock_item")


# --- Write tools: Vouchers ---


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def create_voucher(
    ctx: Context,
    voucher_type: str,
    date: str,
    entries: list[dict],
    company: str,
    party_name: str = "",
    narration: str = "",
    reference: str = "",
    is_invoice: bool = False,
    inventory_entries: list[dict] | None = None,
) -> dict:
    """Create a voucher in Tally.

    voucher_type: Sales, Purchase, Payment, Receipt, Journal, Contra, Credit Note, Debit Note
    date: YYYYMMDD format
    entries: list of {ledger_name, amount} dicts. Positive = debit, negative = credit. Must sum to 0.
    inventory_entries: optional list of {stock_item_name, quantity, rate, amount, godown} for item invoices

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    voucher = VoucherCreate(
        voucher_type=voucher_type,
        date=date,
        party_name=party_name,
        narration=narration,
        reference=reference,
        is_invoice=is_invoice,
        entries=[VoucherEntry(**e) for e in entries],
        inventory_entries=[InventoryEntry(**ie) for ie in (inventory_entries or [])],
    )
    xml = build_voucher_xml(voucher)
    return await _confirm_and_send(ctx, xml, "Vouchers", company, tool_name="create_voucher")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def cancel_voucher(
    ctx: Context,
    voucher_type: str,
    voucher_number: str,
    date: str,
    company: str,
) -> dict:
    """Cancel a voucher in Tally by type, number, and date.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    safe_type = saxutils.escape(voucher_type, {'"': '&quot;'})
    safe_number = saxutils.escape(voucher_number)
    safe_date = saxutils.escape(date)
    xml = f'''<VOUCHER VCHTYPE="{safe_type}" ACTION="Cancel">
    <VOUCHERNUMBER>{safe_number}</VOUCHERNUMBER>
    <DATE>{safe_date}</DATE>
    <VOUCHERTYPENAME>{safe_type}</VOUCHERTYPENAME>
    </VOUCHER>'''
    return await _confirm_and_send(ctx, xml, "Vouchers", company, tool_name="cancel_voucher")


# --- Bulk tools ---


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def bulk_create_ledgers(
    ctx: Context,
    ledgers_json: str,
    company: str,
) -> dict:
    """Create multiple ledgers at once from a JSON array.

    Each item: {name, parent, opening_balance?, address?, gst_number?}
    This saves tokens vs calling create_ledger multiple times.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    ledgers = json.loads(ledgers_json)
    xml_parts = []
    for l in ledgers:
        ledger = LedgerCreate(**l)
        xml_parts.append(build_ledger_xml(ledger))
    combined_xml = "\n".join(xml_parts)
    return await _confirm_and_send(ctx, combined_xml, "All Masters", company, tool_name="bulk_create_ledgers")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def bulk_create_vouchers(
    ctx: Context,
    vouchers_json: str,
    company: str,
) -> dict:
    """Create multiple vouchers at once from a JSON array.

    Each item follows the create_voucher schema.
    This saves tokens vs calling create_voucher multiple times.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    vouchers = json.loads(vouchers_json)
    xml_parts = []
    for v in vouchers:
        v["entries"] = [VoucherEntry(**e) for e in v["entries"]]
        if "inventory_entries" in v:
            v["inventory_entries"] = [InventoryEntry(**ie) for ie in v["inventory_entries"]]
        voucher = VoucherCreate(**v)
        xml_parts.append(build_voucher_xml(voucher))
    combined_xml = "\n".join(xml_parts)
    return await _confirm_and_send(ctx, combined_xml, "Vouchers", company, tool_name="bulk_create_vouchers")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def import_csv_ledgers(
    ctx: Context,
    csv_data: str,
    company: str,
) -> dict:
    """Import ledgers from CSV string. Columns: name, parent, opening_balance, address, gst_number.

    Useful for bulk importing ledgers from spreadsheets.

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    reader = csv.DictReader(io.StringIO(csv_data))
    xml_parts = []
    for row in reader:
        ledger = LedgerCreate(
            name=row["name"],
            parent=row["parent"],
            opening_balance=float(row.get("opening_balance", 0) or 0),
            address=row.get("address", ""),
            gst_number=row.get("gst_number", ""),
        )
        xml_parts.append(build_ledger_xml(ledger))
    combined_xml = "\n".join(xml_parts)
    return await _confirm_and_send(ctx, combined_xml, "All Masters", company, tool_name="import_csv_ledgers")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def import_csv_vouchers(
    ctx: Context,
    csv_data: str,
    voucher_type: str,
    company: str,
) -> dict:
    """Import vouchers from CSV. Each row is one entry; rows with the same 'reference' are grouped into one voucher.

    Required columns: reference, date, ledger_name, amount, narration
    Optional: party_name, cost_centre

    Example CSV:
    reference,date,ledger_name,amount,narration,party_name
    INV001,20250415,Sales Account,-5000,April sale,Customer A
    INV001,20250415,Customer A,5000,April sale,Customer A

    The company parameter is required to prevent accidental writes to the wrong company.
    """
    reader = csv.DictReader(io.StringIO(csv_data))
    groups: dict[str, list[dict]] = {}
    for row in reader:
        ref = row["reference"]
        groups.setdefault(ref, []).append(row)

    xml_parts = []
    for ref, rows in groups.items():
        first = rows[0]
        entries = [
            VoucherEntry(
                ledger_name=r["ledger_name"],
                amount=float(r["amount"]),
                cost_centre=r.get("cost_centre", ""),
            )
            for r in rows
        ]
        voucher = VoucherCreate(
            voucher_type=voucher_type,
            date=first["date"],
            party_name=first.get("party_name", ""),
            narration=first.get("narration", ""),
            reference=ref,
            entries=entries,
        )
        xml_parts.append(build_voucher_xml(voucher))

    combined_xml = "\n".join(xml_parts)
    return await _confirm_and_send(ctx, combined_xml, "Vouchers", company, tool_name="import_csv_vouchers")


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
async def execute_custom_query(
    ctx: Context,
    xml_request: str,
    company: str = "",
) -> str:
    """Execute a custom XML request against Tally. For advanced users who need raw XML access.

    Send a complete Tally XML envelope and get the raw XML response back.
    WARNING: This bypasses confirmation for non-import requests. Import requests
    will still show a confirmation summary.
    """
    _check_role(company, require_write=True, tool_name="execute_custom_query")
    xml_bytes = xml_request.encode("utf-8")

    # If this looks like an import request, confirm it
    if b"<TALLYREQUEST>Import Data</TALLYREQUEST>" in xml_bytes:
        summary = summarize_import_xml(xml_bytes)
        result = await ctx.elicit(
            f"Review the following custom import before sending to Tally:\n\n{summary}\n\nProceed?",
            response_type=None,
        )
        if result.action != "accept":
            return "Operation cancelled by user."

    response = await get_client()._post(xml_bytes)
    return response.decode("utf-8", errors="replace")


# --- Entry point ---


def _cli_auth(args: list[str]) -> None:
    """Deprecated — redirects to 'tally-mcp user'."""
    print("DEPRECATED: 'tally-mcp auth' is replaced by 'tally-mcp user'.")
    print("Please use: tally-mcp user <add|list|grant|revoke|cycle|revoke-all>")
    sys.exit(1)


def _cli_user(args: list[str]) -> None:
    """Handle `tally-mcp user <subcommand>` from the CLI."""
    from tally_mcp.company_config import _config_dir
    from tally_mcp.user_manager import UserManager

    um = UserManager(_config_dir())

    if not args:
        print("Usage: tally-mcp user <add|list|grant|revoke|cycle|revoke-all>")
        sys.exit(1)

    subcmd = args[0]

    if subcmd == "add":
        if len(args) < 2:
            print("Usage: tally-mcp user add <name> --role <read|write|admin> [--companies \"Co A,Co B\"]")
            sys.exit(1)
        name = args[1]
        role = "read"
        companies_str = ""
        i = 2
        while i < len(args):
            if args[i] == "--role" and i + 1 < len(args):
                role = args[i + 1]
                i += 2
            elif args[i] == "--companies" and i + 1 < len(args):
                companies_str = args[i + 1]
                i += 2
            else:
                i += 1

        # Admin defaults to wildcard, others require --companies or get wildcard with warning
        if role == "admin":
            companies = ["*"]
        elif companies_str:
            companies = [c.strip() for c in companies_str.split(",")]
        else:
            companies = ["*"]
            print(f"WARNING: No --companies specified. User '{name}' will have access to ALL companies.")
            print("         Use --companies to restrict access.\n")

        try:
            token = um.add_user(name, role, companies)
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"User '{name}' created (role: {role}).")
        if companies != ["*"]:
            print(f"  Companies: {', '.join(companies)}")
        else:
            print("  Companies: ALL")
        print(f"  Token: {token}")
        print("\nWARNING: Save this token now. It cannot be retrieved later, only cycled.")

    elif subcmd == "list":
        users = um.list_users()
        if not users:
            print("No users configured.")
            return
        print(f"{'Name':<20} {'Role':<8} {'Token':<18} {'Companies'}")
        print("-" * 80)
        for u in users:
            scope = ", ".join(u["companies"]) if u["companies"] != ["*"] else "ALL"
            print(f"{u['name']:<20} {u['role']:<8} {u['token_hint']:<18} {scope}")

    elif subcmd == "grant":
        if len(args) < 3:
            print('Usage: tally-mcp user grant <name> "<company name>"')
            sys.exit(1)
        name = args[1]
        company = args[2]
        if um.grant_company(name, company):
            print(f"Granted '{company}' access to user '{name}'.")
        else:
            user = um.get_by_name(name)
            if not user:
                print(f"Error: User '{name}' not found.")
            elif "*" in user.companies:
                print(f"User '{name}' already has wildcard access to all companies.")
            else:
                print(f"User '{name}' already has access to '{company}'.")

    elif subcmd == "revoke":
        if len(args) < 2:
            print("Usage: tally-mcp user revoke <name>")
            sys.exit(1)
        name = args[1]
        if um.revoke_user(name):
            print(f"User '{name}' revoked. Their token is no longer valid.")
        else:
            print(f"Error: User '{name}' not found.")
            sys.exit(1)

    elif subcmd == "cycle":
        if len(args) < 2:
            print("Usage: tally-mcp user cycle <name>")
            sys.exit(1)
        name = args[1]
        new_token = um.cycle_token(name)
        if new_token:
            print(f"Token cycled for user '{name}'.")
            print(f"  New token: {new_token}")
            print("WARNING: Old token is revoked. Save the new token now.")
        else:
            print(f"Error: User '{name}' not found.")
            sys.exit(1)

    elif subcmd == "revoke-all":
        reg = um.load()
        if not reg.users:
            print("No users to revoke.")
            return
        print(f"WARNING: This will revoke ALL {len(reg.users)} user(s).")
        print("The server will start in open-access mode until new users are added.")
        confirm = input("Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Cancelled.")
            return
        count = um.revoke_all()
        print(f"Revoked {count} user(s). All tokens are now invalid.")

    else:
        print(f"Unknown subcommand: {subcmd}")
        print("Usage: tally-mcp user <add|list|grant|revoke|cycle|revoke-all>")
        sys.exit(1)


def main():
    # Handle CLI subcommands before starting the server
    if len(sys.argv) > 1 and sys.argv[1] == "user":
        _cli_user(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        _cli_auth(sys.argv[2:])
        return

    transport = "stdio"
    if "--http" in sys.argv or os.environ.get("TALLY_MCP_TRANSPORT") == "http":
        transport = "streamable-http"

    if transport == "streamable-http":
        from tally_mcp.user_manager import UserManager
        um = UserManager(_config_dir())
        if not um.load().users:
            print("WARNING: No users configured. Server starting without authentication.")
            print("         Run: tally-mcp user add <name> --role admin")
            print("NOTE: Adding/removing users requires a server restart to take effect.")
            print()

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            host=settings.server_host,
            port=settings.server_port,
        )


if __name__ == "__main__":
    main()
