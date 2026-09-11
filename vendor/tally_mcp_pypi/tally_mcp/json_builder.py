"""Build JSON requests for TallyPrime 7.0+ JSONEx interface.

All functions return (headers: dict, body: dict) tuples ready for httpx.post().
Falls back gracefully — callers should catch failures and retry with XML.
"""

from tally_mcp.xml_builder import REPORT_NAME_MAP


def _static_vars(
    company: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
    export_format: str = "JSONEx",
) -> list[dict]:
    """Build the static_variables list for a JSON request."""
    vs = [{"name": "svExportFormat", "value": export_format}]
    if company:
        vs.append({"name": "svCurrentCompany", "value": company})
    if from_date:
        vs.append({"name": "svFromDate", "value": from_date})
    if to_date:
        vs.append({"name": "svToDate", "value": to_date})
    if extra_vars:
        for k, v in extra_vars.items():
            vs.append({"name": k, "value": v})
    return vs


def build_json_export_report(
    report_name: str,
    company: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
) -> tuple[dict, dict]:
    """Build a JSON report export request (Trial Balance, P&L, etc.)."""
    json_name = REPORT_NAME_MAP.get(report_name, report_name)
    headers = {
        "content-type": "application/json",
        "version": "1",
        "tallyrequest": "Export",
        "type": "Data",
        "id": json_name,
    }
    body = {
        "static_variables": _static_vars(company, from_date, to_date, extra_vars),
    }
    return headers, body


def build_json_export_collection(
    collection_name: str,
    company: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
    tdl: str = "",
) -> tuple[dict, dict]:
    """Build a JSON collection export request (ledgers, groups, etc.).

    NOTE: Custom TDL collections are NOT supported via JSON — they require
    XML. If tdl is non-empty, the caller should fall back to XML.
    """
    headers = {
        "content-type": "application/json",
        "version": "1",
        "tallyrequest": "Export",
        "type": "Collection",
        "id": collection_name,
    }
    body = {
        "static_variables": _static_vars(company, from_date, to_date, extra_vars),
    }
    return headers, body


def build_json_import_masters(
    data: dict,
    company: str = "",
) -> tuple[dict, dict]:
    """Build a JSON master import request (ledgers, groups, stock items)."""
    headers = {
        "content-type": "application/json",
        "version": "1",
        "tallyrequest": "Import",
        "type": "Data",
        "id": "All Masters",
    }
    vs = [
        {"name": "svMstImportFormat", "value": "JSONEx"},
    ]
    if company:
        vs.append({"name": "svCurrentCompany", "value": company})
    body = {
        "static_variables": vs,
        "tallymessage": data,
    }
    return headers, body


def build_json_import_vouchers(
    data: dict,
    company: str = "",
) -> tuple[dict, dict]:
    """Build a JSON voucher import request."""
    headers = {
        "content-type": "application/json",
        "version": "1",
        "tallyrequest": "Import",
        "type": "Data",
        "id": "Vouchers",
    }
    vs = [
        {"name": "svVchImportFormat", "value": "JSONEx"},
    ]
    if company:
        vs.append({"name": "svCurrentCompany", "value": company})
    body = {
        "static_variables": vs,
        "tallymessage": data,
    }
    return headers, body
