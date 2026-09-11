import json as json_mod
import logging

import httpx

from tally_mcp.json_builder import (
    build_json_export_collection,
    build_json_export_report,
)
from tally_mcp.json_parser import (
    is_json_supported,
    parse_json_collection,
    parse_json_import_result,
    parse_json_report,
)
from tally_mcp.xml_builder import (
    build_export_collection_request,
    build_export_report_request,
    build_export_report_request_v7,
    build_import_request,
)
from tally_mcp.xml_parser import parse_collection, parse_import_result, parse_report_raw

log = logging.getLogger(__name__)


class TallyError(Exception):
    pass


class TallyClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 9000,
        default_company: str = "",
        username: str = "",
        password: str = "",
    ):
        self.url = f"http://{host}:{port}"
        self.default_company = default_company
        self.username = username
        self.password = password
        # None = not yet detected, True/False = detected
        self._json_supported: bool | None = None

    # ── Low-level HTTP ────────────────────────────────────────────

    async def _post_xml(self, xml_data: bytes) -> bytes:
        """POST XML to Tally, return raw bytes response."""
        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.post(
                self.url,
                content=xml_data,
                headers={"Content-Type": "text/xml;charset=utf-8"},
            )
        if response.status_code != 200:
            raise TallyError(
                f"Tally returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.content

    # Keep _post as alias for backwards compatibility (scripts, tests)
    _post = _post_xml

    async def _post_json(self, headers: dict, body: dict) -> dict:
        """POST JSON to Tally, return parsed dict response."""
        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.post(
                self.url,
                headers=headers,
                json=body,
            )
        if response.status_code != 200:
            raise TallyError(
                f"Tally returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    async def _detect_json(self) -> bool:
        """Auto-detect whether this Tally instance supports JSONEx.

        Sends a minimal JSON collection request. If Tally returns a valid
        JSON response, JSONEx is supported. Otherwise falls back to XML.
        """
        if self._json_supported is not None:
            return self._json_supported
        try:
            headers = {
                "content-type": "application/json",
                "version": "1",
                "tallyrequest": "Export",
                "type": "Collection",
                "id": "List of Companies",
            }
            body = {
                "static_variables": [
                    {"name": "svExportFormat", "value": "JSONEx"},
                ],
            }
            data = await self._post_json(headers, body)
            self._json_supported = is_json_supported(data)
        except Exception:
            self._json_supported = False
        transport = "JSONEx" if self._json_supported else "XML"
        log.info("Tally transport: %s", transport)
        return self._json_supported

    @property
    def transport(self) -> str:
        """Return current transport type string."""
        if self._json_supported is True:
            return "json"
        return "xml"

    def _company(self, company: str | None) -> str:
        return company or self.default_company

    # ── Health & heartbeat ────────────────────────────────────────

    async def health_check(self) -> dict:
        companies = await self.list_companies()
        return {"status": "ok", "companies": [c["NAME"] for c in companies]}

    async def heartbeat(self, company: str | None = None) -> dict:
        """Quick connectivity check, optionally verifying a specific company is open.

        Returns {"ok": True/False, "company": str, "message": str, "transport": str}.
        """
        target = self._company(company)
        try:
            await self._detect_json()
            health = await self.health_check()
        except Exception as exc:
            return {"ok": False, "company": target, "message": f"Tally unreachable: {exc}",
                    "transport": self.transport}
        if target and target not in health["companies"]:
            return {
                "ok": False,
                "company": target,
                "message": f"Company '{target}' not open. Open companies: {health['companies']}",
                "transport": self.transport,
            }
        return {"ok": True, "company": target, "message": "connected",
                "transport": self.transport}

    # ── Collection exports ────────────────────────────────────────

    async def list_companies(self) -> list[dict]:
        await self._detect_json()
        if self._json_supported:
            return await self._list_companies_json()
        return await self._list_companies_xml()

    async def _list_companies_json(self) -> list[dict]:
        headers, body = build_json_export_collection("List of Companies")
        data = await self._post_json(headers, body)
        return parse_json_collection(data, "COMPANY")

    async def _list_companies_xml(self) -> list[dict]:
        xml = build_export_collection_request(
            collection_name="List of Companies",
            company="",
        )
        response = await self._post_xml(xml)
        return parse_collection(response, "COMPANY")

    async def export_collection(
        self,
        collection_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> list[dict]:
        await self._detect_json()
        # Custom TDL collections must use XML (JSON doesn't support inline TDL)
        if self._json_supported and not tdl:
            return await self._export_collection_json(
                collection_name, element_tag, company, from_date, to_date, extra_vars,
            )
        return await self._export_collection_xml(
            collection_name, element_tag, company, from_date, to_date, extra_vars, tdl,
        )

    async def _export_collection_json(
        self,
        collection_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict]:
        headers, body = build_json_export_collection(
            collection_name=collection_name,
            company=self._company(company),
            from_date=from_date,
            to_date=to_date,
            extra_vars=extra_vars,
        )
        data = await self._post_json(headers, body)
        return parse_json_collection(data, element_tag)

    async def _export_collection_xml(
        self,
        collection_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> list[dict]:
        xml = build_export_collection_request(
            collection_name=collection_name,
            company=self._company(company),
            username=self.username,
            password=self.password,
            from_date=from_date,
            to_date=to_date,
            extra_vars=extra_vars,
            tdl=tdl,
        )
        response = await self._post_xml(xml)
        return parse_collection(response, element_tag)

    # ── Report exports ────────────────────────────────────────────

    async def export_report(
        self,
        report_name: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> str | dict:
        """Export a report. Returns dict (JSON) or str (XML) depending on transport."""
        await self._detect_json()
        if self._json_supported and not tdl:
            return await self._export_report_json(
                report_name, company, from_date, to_date, extra_vars,
            )
        return await self._export_report_xml(
            report_name, company, from_date, to_date, extra_vars, tdl,
        )

    async def _export_report_json(
        self,
        report_name: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
    ) -> dict:
        headers, body = build_json_export_report(
            report_name=report_name,
            company=self._company(company),
            from_date=from_date,
            to_date=to_date,
            extra_vars=extra_vars,
        )
        data = await self._post_json(headers, body)
        return parse_json_report(data)

    async def _export_report_xml(
        self,
        report_name: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> str:
        # Use v7 report name mapping when TallyPrime 7.0+ is detected
        builder = build_export_report_request_v7 if self._json_supported else build_export_report_request
        xml = builder(
            report_name=report_name,
            company=self._company(company),
            username=self.username,
            password=self.password,
            from_date=from_date,
            to_date=to_date,
            extra_vars=extra_vars,
            tdl=tdl,
        )
        response = await self._post_xml(xml)
        return parse_report_raw(response)

    # ── Imports ───────────────────────────────────────────────────

    def build_import_xml(
        self,
        data_xml: str,
        request_type: str = "All Masters",
        company: str | None = None,
    ) -> bytes:
        """Build the import XML envelope without sending it.

        Use this to inspect/summarize the request before executing.
        """
        return build_import_request(
            data_xml=data_xml,
            request_type=request_type,
            company=self._company(company),
            username=self.username,
            password=self.password,
        )

    async def send_import(self, xml: bytes) -> dict:
        """Send a pre-built import XML envelope to Tally."""
        response = await self._post_xml(xml)
        return parse_import_result(response)

    async def import_data(
        self,
        data_xml: str,
        request_type: str = "All Masters",
        company: str | None = None,
    ) -> dict:
        xml = self.build_import_xml(data_xml, request_type, company)
        return await self.send_import(xml)

    # ── Force transport (for testing) ─────────────────────────────

    async def export_collection_xml(
        self,
        collection_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> list[dict]:
        """Force XML transport for collection export (for testing)."""
        return await self._export_collection_xml(
            collection_name, element_tag, company, from_date, to_date, extra_vars, tdl,
        )

    async def export_collection_json(
        self,
        collection_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict]:
        """Force JSON transport for collection export (for testing)."""
        return await self._export_collection_json(
            collection_name, element_tag, company, from_date, to_date, extra_vars,
        )

    async def export_report_xml(
        self,
        report_name: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> str:
        """Force XML transport for report export (for testing)."""
        return await self._export_report_xml(
            report_name, company, from_date, to_date, extra_vars, tdl,
        )

    async def export_report_json(
        self,
        report_name: str,
        company: str | None = None,
        from_date: str = "",
        to_date: str = "",
        extra_vars: dict[str, str] | None = None,
    ) -> dict:
        """Force JSON transport for report export (for testing)."""
        return await self._export_report_json(
            report_name, company, from_date, to_date, extra_vars,
        )
