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


#: Voucher-level fields worth copying onto every one of its ledger rows.
_VOUCHER_FIELDS = (
    "DATE",
    "VOUCHERTYPENAME",
    "VOUCHERNUMBER",
    "PARTYLEDGERNAME",
    "REFERENCE",
    "NARRATION",
    "MASTERID",
    "GUID",
)


def parse_voucher_rows(xml_bytes: bytes) -> list[dict[str, object]]:
    """Flatten a voucher collection into one row per ledger entry.

    ``parse_collection`` cannot do this: ``ALLLEDGERENTRIES.LIST`` contains
    nested lists of its own (bill and cost-centre allocations), so the generic
    walker serialises it to a string. Day-book-shaped reports want one row per
    posting, which is what this produces.

    Amounts stay in **Tally's** convention here (negated, debit negative); the
    backend flips them to ours at the same boundary as everything else.

    Scoped to ``<COLLECTION>`` on purpose: every Tally response also carries a
    ``<CMPINFO>`` block containing a ``<VOUCHER>`` element that is an object
    *count*, not a voucher. Iterating the whole document picks it up and emits a
    phantom empty row.
    """
    root = parse(xml_bytes)
    collection = root.find(".//COLLECTION")
    if collection is None:
        return []
    rows: list[dict[str, object]] = []
    for voucher in collection.iter("VOUCHER"):
        shared: dict[str, object] = {
            field: (voucher.findtext(field) or "").strip()
            for field in _VOUCHER_FIELDS
        }
        if not shared.get("VOUCHERTYPENAME"):
            shared["VOUCHERTYPENAME"] = voucher.get("VCHTYPE", "")
        # REMOTEID is a GUID and lives on the attribute, not as a child. It is
        # the id an amendment should be aimed at.
        shared["REMOTEID"] = voucher.get("REMOTEID", "")
        if not shared.get("GUID"):
            shared["GUID"] = voucher.get("REMOTEID", "")

        entries = list(voucher.iter("ALLLEDGERENTRIES.LIST"))
        if not entries:
            rows.append(dict(shared))
            continue
        for entry in entries:
            row = dict(shared)
            row["LEDGERNAME"] = (entry.findtext("LEDGERNAME") or "").strip()
            row["AMOUNT"] = (entry.findtext("AMOUNT") or "").strip()
            row["ISDEEMEDPOSITIVE"] = (
                entry.findtext("ISDEEMEDPOSITIVE") or ""
            ).strip()
            row["BILLS"] = _bill_allocations(entry)
            rows.append(row)
    return rows


def parse_stock_rows(xml_bytes: bytes) -> list[dict[str, object]]:
    """Flatten a voucher collection into one row per *inventory* entry.

    The accounting flattening (``parse_voucher_rows``) walks
    ALLLEDGERENTRIES; this walks ALLINVENTORYENTRIES, which is a separate
    dimension of the same voucher. Quantities and amounts stay in Tally's
    convention and are flipped at the backend boundary like everything else.
    """
    root = parse(xml_bytes)
    collection = root.find(".//COLLECTION")
    if collection is None:
        return []

    rows: list[dict[str, object]] = []
    for voucher in collection.iter("VOUCHER"):
        shared: dict[str, object] = {
            field: (voucher.findtext(field) or "").strip()
            for field in _VOUCHER_FIELDS
        }
        if not shared.get("VOUCHERTYPENAME"):
            shared["VOUCHERTYPENAME"] = voucher.get("VCHTYPE", "")

        for entry in voucher.iter("ALLINVENTORYENTRIES.LIST"):
            row = dict(shared)
            row["STOCKITEMNAME"] = (entry.findtext("STOCKITEMNAME") or "").strip()
            row["ACTUALQTY"] = (entry.findtext("ACTUALQTY") or "").strip()
            row["BILLEDQTY"] = (entry.findtext("BILLEDQTY") or "").strip()
            row["RATE"] = (entry.findtext("RATE") or "").strip()
            row["AMOUNT"] = (entry.findtext("AMOUNT") or "").strip()
            row["GODOWNNAME"] = ""
            for batch in entry.iter("BATCHALLOCATIONS.LIST"):
                godown = (batch.findtext("GODOWNNAME") or "").strip()
                if godown:
                    row["GODOWNNAME"] = godown
                    break
            rows.append(row)
    return rows


def _bill_allocations(entry: etree._Element) -> list[dict[str, str]]:
    """Bill-wise allocations hanging off one ledger entry.

    This is what makes outstanding-by-bill possible: Tally tracks receivables
    against a bill reference, not just a party balance. ``BILLTYPE`` is
    ``New Ref`` when a bill is raised, ``Agst Ref`` when one is settled, and
    ``On Account`` when the entry is not against a specific bill at all.
    """
    bills: list[dict[str, str]] = []
    for allocation in entry.iter("BILLALLOCATIONS.LIST"):
        name = (allocation.findtext("NAME") or "").strip()
        bill_type = (allocation.findtext("BILLTYPE") or "").strip()
        amount = (allocation.findtext("AMOUNT") or "").strip()
        if not name and not amount:
            continue
        bills.append({"name": name, "type": bill_type, "amount": amount})
    return bills


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


def _first_id(container: etree._Element, *names: str) -> str:
    """An id from Tally, where ``0`` means "none".

    A voucher import returns ``<LASTMID>0</LASTMID>`` because master ids are for
    masters; taking that literally hands ``"0"`` to an alter, which Tally then
    cannot match - and silently creates a *new* voucher instead.
    """
    value = _first_text(container, *names)
    return "" if value.strip() in ("", "0") else value


@dataclass(slots=True)
class ImportResult:
    created: int = 0
    altered: int = 0
    ignored: int = 0
    exceptions: int = 0
    errors_reported: int = 0
    last_voucher_id: str = ""
    last_master_id: str = ""
    errors: list[LineError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.errors
            and self.exceptions == 0
            and self.errors_reported == 0
            and bool(self.created or self.altered)
        )

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
        errors_reported=_first_int(container, "ERRORS"),
        last_voucher_id=_first_id(container, "LASTVCHID"),
        last_master_id=_first_id(container, "LASTMASTERID", "LASTMID"),
    )
    result.errors = [
        LineError(err.text.strip()) for err in root.iter("LINEERROR") if err.text
    ]
    if result.errors_reported and not result.errors:
        result.errors.append(
            LineError(f"Tally reported {result.errors_reported} error(s) on import")
        )
    if result.exceptions and not result.errors:
        # Exceptions with no LINEERROR: LASTERROR is the only detail Tally gives.
        last_error = _first_text(container, "LASTERROR")
        result.errors.append(
            LineError(
                last_error or f"Tally reported {result.exceptions} exception(s)"
            )
        )
    return result
