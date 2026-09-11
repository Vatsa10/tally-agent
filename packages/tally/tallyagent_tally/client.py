"""Async TallyPrime XML-over-HTTP client.

One HTTP verb, one endpoint: POST an ENVELOPE to http://host:port/. Tally
answers 200 with XML even for logical failures, so "did it work" is decided by
parsing the body, never by the status code.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx
from lxml import etree

from tallyagent_core.errors import ConnectionError_, TallyError
from tallyagent_tally.xml import builders, parsers
from tallyagent_tally.xml.quirks import to_tally_date

log = logging.getLogger(__name__)

_VERSION = re.compile(r"(?:TallyPrime|Tally)[^0-9]{0,20}([0-9]+(?:\.[0-9]+)*)", re.I)


@dataclass(slots=True)
class TallyConfig:
    # 0.0.0.0 is the shipped default on purpose: a fresh install must not be
    # able to reach a real Tally until someone edits config.toml.
    host: str = "0.0.0.0"
    port: int = 9000
    company: str = ""
    username: str = ""
    password: str = ""
    timeout_seconds: float = 60.0

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class TallyClient:
    """Transport plus the handful of primitives everything else is built from."""

    def __init__(
        self,
        config: TallyConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or TallyConfig()
        # Injected transport is how tests and the fake server attach without a
        # socket ever being opened.
        self._transport = transport
        self._version: str | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.config.timeout_seconds,
            transport=self._transport,
        )

    def company_or_default(self, company: str | None) -> str:
        return company or self.config.company

    async def post(self, payload: bytes) -> bytes:
        """POST one envelope. Raises ConnectionError_/TallyError, never returns
        a failure sentinel."""
        try:
            async with self._client() as http:
                response = await http.post(
                    self.config.url,
                    content=payload,
                    headers={"Content-Type": "text/xml; charset=utf-16"},
                )
        except httpx.RequestError as exc:
            raise ConnectionError_(self.config.url, str(exc)) from exc
        if response.status_code != 200:
            raise TallyError(
                f"Tally returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.content

    # --- probe --------------------------------------------------------------

    async def probe(self) -> dict[str, str]:
        """Is Tally there, what version, which companies are open?

        Tally answers a bare GET with an HTML-ish banner carrying its version.
        A failure to parse the version is not fatal - the companies list is the
        real proof of life.
        """
        version = ""
        try:
            async with self._client() as http:
                banner = await http.get(self.config.url)
            match = _VERSION.search(banner.text)
            if match:
                version = match.group(1)
        except httpx.RequestError as exc:
            raise ConnectionError_(self.config.url, str(exc)) from exc

        companies = await self.list_companies()
        self._version = version
        return {
            "url": self.config.url,
            "version": version or "unknown",
            "companies": ", ".join(companies),
            "status": "ok",
        }

    async def list_companies(self) -> list[str]:
        payload = builders.build_export_collection("List of Companies")
        return parsers.parse_companies(await self.post(payload))

    # --- exports ------------------------------------------------------------

    async def export_collection(
        self,
        collection_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        extra_vars: dict[str, str] | None = None,
        tdl: str = "",
    ) -> list[dict[str, object]]:
        payload = builders.build_export_collection(
            collection_name=collection_name,
            company=self.company_or_default(company),
            username=self.config.username,
            password=self.config.password,
            from_date=to_tally_date(from_date) if from_date else "",
            to_date=to_tally_date(to_date) if to_date else "",
            extra_vars=extra_vars,
            tdl=tdl,
        )
        return parsers.parse_collection(await self.post(payload), element_tag)

    async def export_report(
        self,
        report_name: str,
        element_tag: str,
        company: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> list[dict[str, object]]:
        payload = builders.build_export_report(
            report_name=report_name,
            company=self.company_or_default(company),
            username=self.config.username,
            password=self.config.password,
            from_date=to_tally_date(from_date) if from_date else "",
            to_date=to_tally_date(to_date) if to_date else "",
            extra_vars=extra_vars,
        )
        return parsers.parse_collection(await self.post(payload), element_tag)

    # --- imports ------------------------------------------------------------

    async def import_elements(
        self,
        elements: list[etree._Element],
        request_type: str,
        company: str | None = None,
    ) -> tuple[parsers.ImportResult, str]:
        """Send an import and return (result, the raw XML we sent).

        The raw request comes back so the approval record can show exactly what
        was transmitted - "what did you send Tally" must always be answerable.
        """
        payload = builders.build_import(
            payload=elements,
            request_type=request_type,
            company=self.company_or_default(company),
            username=self.config.username,
            password=self.config.password,
        )
        response = await self.post(payload)
        return parsers.parse_import_result(response), payload.decode("utf-8")
