"""Tally XML response parsers.

Collection walking and import-result extraction ported from
vendor/tally_mcp_pypi/tally_mcp/xml_parser.py (MIT), with the import result
promoted from a loose dict to a typed ``ImportResult`` that carries structured
``<LINEERROR>`` text - the vendored version returns bare strings, and the whole
point of our adapter is that a failed post produces an actionable error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from lxml import etree

from tallyagent_tally.xml.quirks import clean_response


def parse(xml_bytes: bytes) -> etree._Element:
    return etree.fromstring(clean_response(xml_bytes))


def _leaf_text(node: etree._Element) -> str:
    return node.text.strip() if node.text else ""


def parse_collection(xml_bytes: bytes, element_tag: str) -> list[dict[str, object]]:
    """Flatten ``<COLLECTION>`` children into dicts.

    Attributes are prefixed with ``@`` (that is where Tally hides NAME and
    REMOTEID). Nested ``.LIST`` children become lists of dicts.
    """
    root = parse(xml_bytes)
    collection = root.find(".//COLLECTION")
    if collection is None:
        return []
    items: list[dict[str, object]] = []
    for elem in collection.iter(element_tag):
        item: dict[str, object] = {
            f"@{name}": value for name, value in elem.attrib.items()
        }
        for child in elem:
            if len(child) == 0:
                item[child.tag] = _leaf_text(child)
            else:
                nested = [
                    {sub.tag: _leaf_text(sub)} for sub in child if len(sub) == 0
                ]
                item[child.tag] = (
                    nested if nested else etree.tostring(child, encoding="unicode")
                )
        items.append(item)
    return items


def parse_companies(xml_bytes: bytes) -> list[str]:
    return [
        str(item.get("NAME") or item.get("@NAME") or "")
        for item in parse_collection(xml_bytes, "COMPANY")
    ]


def to_decimal(raw: object, default: Decimal = Decimal("0")) -> Decimal:
    """Tally amounts arrive as ``"-11800.00"``, ``""``, ``"(-)500"`` or absent."""
    if raw is None:
        return default
    text = str(raw).strip().replace(",", "")
    if not text:
        return default
    negative = text.startswith("(-)")
    text = text.removeprefix("(-)").removeprefix("(").removesuffix(")")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return default
    return -value if negative else value


@dataclass(slots=True)
class LineError:
    """One structured ``<LINEERROR>``.

    Tally's line errors are free text, but the two shapes that matter are
    recognisable and worth classifying, because the fix differs: a missing
    master needs a ledger created, a duplicate needs nothing done at all.
    """

    message: str

    @property
    def kind(self) -> str:
        text = self.message.lower()
        if "does not exist" in text or "could not find" in text:
            return "unknown_master"
        if "duplicate" in text or "already exists" in text:
            return "duplicate"
        if "date" in text and ("period" in text or "range" in text):
            return "period"
        return "unknown"

    @property
    def missing_master(self) -> str | None:
        """Pull the quoted master name out of an unknown-master error."""
        if self.kind != "unknown_master":
            return None
        for quote in ("'", '"'):
            if self.message.count(quote) >= 2:
                return self.message.split(quote)[1]
        return None


@dataclass(slots=True)
class ImportResult:
    created: int = 0
    altered: int = 0
    ignored: int = 0
    exceptions: int = 0
    last_voucher_id: str = ""
    last_master_id: str = ""
    errors: list[LineError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.exceptions == 0 and (self.created or self.altered)

    @property
    def messages(self) -> list[str]:
        return [e.message for e in self.errors]


def _first_text(container: etree._Element, *names: str) -> str:
    for name in names:
        value = container.findtext(name) or container.findtext(f".//{name}")
        if value:
            return value.strip()
    return ""


def _first_int(container: etree._Element, *names: str) -> int:
    value = _first_text(container, *names)
    try:
        return int(value)
    except ValueError:
        return 0


def parse_import_result(xml_bytes: bytes) -> ImportResult:
    """Read an import response.

    Tally puts the counters inside ``<IMPORTRESULT>`` on some versions and
    directly under ``<RESPONSE>`` on others; both are handled.
    """
    root = parse(xml_bytes)
    import_result = root.find(".//IMPORTRESULT")
    container = import_result if import_result is not None else root

    result = ImportResult(
        created=_first_int(container, "CREATED"),
        altered=_first_int(container, "ALTERED"),
        ignored=_first_int(container, "IGNORED"),
        exceptions=_first_int(container, "EXCEPTIONS"),
        last_voucher_id=_first_text(container, "LASTVCHID"),
        last_master_id=_first_text(container, "LASTMASTERID", "LASTMID"),
    )
    result.errors = [
        LineError(err.text.strip()) for err in root.iter("LINEERROR") if err.text
    ]
    if result.exceptions and not result.errors:
        # Exceptions with no LINEERROR: LASTERROR is the only detail Tally gives.
        last_error = _first_text(container, "LASTERROR")
        result.errors.append(
            LineError(
                last_error or f"Tally reported {result.exceptions} exception(s)"
            )
        )
    return result
