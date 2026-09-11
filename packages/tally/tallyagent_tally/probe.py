"""Is this Tally ready to be worked on?

``probe`` answers one question with evidence: can tallyagent safely operate this
installation right now. It reads from three places, because no single one tells
the whole story:

* the XML port (reachable, companies loaded, object counts);
* ``tally.ini`` on disk (server mode, port, ODBC);
* the process and window title ("TallyPrime:9000" confirms the live port).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from tallyagent_core.errors import ConnectionError_
from tallyagent_core.livemode import LiveMode
from tallyagent_tally import install as install_mod
from tallyagent_tally.client import TallyClient
from tallyagent_tally.xml import builders, parsers

log = logging.getLogger(__name__)

#: Counters Tally returns in <CMPINFO> on every export. With no company loaded
#: they are all zero, which is how "sitting on Select Company" is detected.
INTERESTING_COUNTS = ("COMPANY", "LEDGER", "GROUP", "VOUCHER", "STOCKITEM")


@dataclass(slots=True)
class ProbeReport:
    url: str = ""
    reachable: bool = False
    banner: str = ""
    version: str = ""
    edition: str = "unknown"  # educational | licensed | unknown
    server_mode: bool = False
    odbc: bool = False
    configured_port: int = 0
    window_port: int | None = None
    install_dir: str = ""
    data_dir: str = ""
    companies_loaded: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    write_scope: str = ""
    problems: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.reachable and not self.problems

    @property
    def has_company(self) -> bool:
        return bool(self.companies_loaded)

    def lines(self) -> list[str]:
        """Human-readable, one fact per line, PASS/FAIL last."""
        mark = lambda ok: "ok " if ok else "NO "  # noqa: E731 - a tiny formatter
        out = [
            f"Tally at {self.url}",
            f"  {mark(self.reachable)}reachable          {self.banner or '(no banner)'}",
            f"  {mark(bool(self.version))}version            {self.version or 'unknown'}",
            f"  {'edu' if self.edition == 'educational' else 'lic'} edition            "
            f"{self.edition}",
            f"  {mark(self.server_mode)}server mode        "
            f"{'on' if self.server_mode else 'off'} (tally.ini)",
            f"  {mark(self.odbc)}ODBC               "
            f"{'on' if self.odbc else 'off'} (tally.ini)",
            f"  {mark(bool(self.configured_port))}port               "
            f"{self.configured_port or '?'}"
            + (f" (window says {self.window_port})" if self.window_port else ""),
            f"  {mark(self.has_company)}companies loaded   "
            f"{', '.join(self.companies_loaded) or '(none - Select Company screen)'}",
        ]
        if self.install_dir:
            out.append(f"     install            {self.install_dir}")
        if self.data_dir:
            out.append(f"     data               {self.data_dir}")
        if self.counts:
            out.append(
                "     objects            "
                + ", ".join(f"{k.lower()}={v}" for k, v in self.counts.items())
            )
        if self.write_scope:
            out.append(f"     write scope        {self.write_scope}")
        for problem in self.problems:
            out.append(f"  !  {problem}")
        for tip in self.advice:
            out.append(f"  -> {tip}")
        out.append("")
        out.append("PASS ready for live mode" if self.ready else "FAIL not ready for live mode")
        return out

    def render(self) -> str:
        return "\n".join(self.lines())


def _edition_from_banner(banner: str) -> str:
    lowered = banner.lower()
    if "educational" in lowered or "edu" in lowered.split():
        return "educational"
    if "licen" in lowered or "silver" in lowered or "gold" in lowered:
        return "licensed"
    return "unknown"


def parse_counts(xml_bytes: bytes) -> dict[str, int]:
    """Pull the <CMPINFO> counters out of any export response."""
    try:
        root = parsers.parse(xml_bytes)
    except etree.XMLSyntaxError:
        return {}
    info = root.find(".//CMPINFO")
    if info is None:
        return {}
    counts: dict[str, int] = {}
    for name in INTERESTING_COUNTS:
        text = info.findtext(name)
        if text and text.strip().isdigit():
            counts[name] = int(text.strip())
    return counts


async def probe(
    client: TallyClient,
    live: LiveMode | None = None,
    install_dir: str | Path | None = None,
) -> ProbeReport:
    """Gather everything, judge readiness, never raise on an unreachable Tally."""
    live = live or LiveMode()
    report = ProbeReport(url=client.config.url, write_scope=live.describe())

    # --- the port ---
    try:
        banner = await client.banner()
        report.reachable = True
        report.banner = banner.strip()
    except ConnectionError_ as exc:
        report.problems.append(f"cannot reach {client.config.url}: {exc.details}")

    # --- the installation on disk ---
    installation = install_mod.find_install(install_dir)
    if installation is None:
        report.advice.append(
            "no TallyPrime installation found on this machine; only the port was checked"
        )
    else:
        report.install_dir = str(installation.install_dir)
        report.version = installation.version
        report.data_dir = str(installation.data_dir or "")
        connectivity = install_mod.read_connectivity(installation.ini)
        report.server_mode = connectivity.server_mode
        report.odbc = connectivity.odbc
        report.configured_port = connectivity.port
        if not connectivity.server_mode:
            report.problems.append(
                f"tally.ini says {install_mod.KEY_CLIENT_SERVER}="
                f"{connectivity.client_server or '(unset)'}; the XML port will not listen"
            )
            report.advice.append("run /enable-server (or: tallyagent enable-server)")
        if connectivity.port and connectivity.port != client.config.port:
            report.problems.append(
                f"tally.ini port {connectivity.port} does not match the configured "
                f"port {client.config.port}"
            )

    report.window_port = install_mod.configured_port_from_title()

    if not report.reachable:
        if install_mod.is_running():
            report.advice.append(
                "TallyPrime is running but the port is closed - run /enable-server"
            )
        elif installation is not None:
            report.advice.append("TallyPrime is not running - start it, then probe again")
        report.edition = "unknown"
        return report

    # --- what the port knows ---
    try:
        raw = await client.post(builders.build_export_collection("List of Companies"))
        report.companies_loaded = [name for name in parsers.parse_companies(raw) if name]
        report.counts = parse_counts(raw)
    except Exception as exc:  # noqa: BLE001 - probe reports, never raises
        report.problems.append(f"company list failed: {type(exc).__name__}: {exc}")

    report.edition = _edition_from_banner(report.banner)
    if report.edition == "unknown" and live.edu:
        # The banner does not name the edition on TallyPrime 1.x. Config is then
        # the only signal, and being told "educational" is authoritative.
        report.edition = "educational"

    if not report.companies_loaded:
        report.advice.append(
            "no company is loaded - use /bootstrap to create one, or open one in Tally"
        )
    elif live.enabled:
        outside = [c for c in report.companies_loaded if not live.may_write_to(c)]
        if outside:
            report.advice.append(
                f"loaded but NOT writable (outside {live.write_prefix}*): "
                + ", ".join(outside)
            )
    return report
