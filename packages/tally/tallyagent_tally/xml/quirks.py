"""TallyPrime's XML quirks, in one place.

Ported from vendor/tally_mcp_pypi/tally_mcp/xml_parser.py (MIT) plus the sign
conventions from its voucher_builder.py. Every function here exists because
real Tally does something a spec-compliant XML parser refuses to accept.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from tallyagent_core.models import VoucherType

# --- dates ------------------------------------------------------------------

#: Tally speaks YYYYMMDD and nothing else. ISO dates are silently ignored,
#: which is worse than an error because the voucher posts on today's date.
TALLY_DATE = "%Y%m%d"


def to_tally_date(when: date) -> str:
    return when.strftime(TALLY_DATE)


def from_tally_date(raw: str) -> date | None:
    """Parse a Tally date. Returns None rather than raising: Tally emits empty
    and ``-`` date cells in report rows, and a report must not die on one."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in (TALLY_DATE, "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            from datetime import datetime

            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# --- amounts and signs ------------------------------------------------------

#: Voucher types where Tally's "party side" is a debit (money coming to us as a
#: receivable, or cash going out). Used only for sanity-checking a draft's
#: shape; the actual sign per line comes from VoucherLine.amount.
DEBIT_PARTY_TYPES = frozenset({VoucherType.SALES, VoucherType.PAYMENT})


def tally_amount(amount: Decimal) -> tuple[str, str]:
    """Convert our sign convention to Tally's.

    Ours: positive = debit. Tally's: ``<AMOUNT>`` is negated, and
    ``<ISDEEMEDPOSITIVE>Yes`` marks the debit side. This is the single most
    common source of reversed vouchers, so it lives in exactly one function.

    Returns ``(amount_text, isdeemedpositive)``.
    """
    xml_amount = -amount
    return (format(xml_amount, "f"), "Yes" if xml_amount < 0 else "No")


# --- request and response encoding ------------------------------------------

#: Tally echoes back whatever charset the request declares: declare UTF-8 and it
#: answers UTF-8, declare UTF-16 and it answers UTF-16LE. Declaring UTF-16 while
#: actually sending UTF-8 bytes makes it reject *every* request with
#: "Unknown Request, cannot be processed" - no error code, no hint. Verified
#: against a live TallyPrime on 2026-09-11. Pinned here so no call site can get
#: it wrong again.
REQUEST_CONTENT_TYPE = "text/xml; charset=utf-8"

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def looks_utf16(xml_bytes: bytes) -> bool:
    """Detect a UTF-16 response: a BOM, or NUL-interleaved ASCII.

    A Tally that answers UTF-16 sends every ASCII character followed by a NUL,
    so the first bytes of an envelope are roughly one third NUL.
    """
    if xml_bytes[:2] in _UTF16_BOMS:
        return True
    head = xml_bytes[:64]
    return b"\x00" in head and head.count(b"\x00") >= len(head) // 3


def decode_utf16(xml_bytes: bytes) -> bytes:
    """Re-encode a UTF-16 response as UTF-8 so one parser handles both."""
    for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
        try:
            return xml_bytes.decode(encoding).encode("utf-8")
        except UnicodeDecodeError:
            continue
    return xml_bytes


# --- response cleanup -------------------------------------------------------

_INVALID_XML_CHARS = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CHARREF = re.compile(rb"&#(\d+);?")


def _strip_bad_charref(m: re.Match[bytes]) -> bytes:
    value = int(m.group(1))
    if value in (0x9, 0xA, 0xD):  # the only control chars XML permits
        return m.group(0)
    if value < 0x20:
        return b""
    return m.group(0)


def clean_response(xml_bytes: bytes) -> bytes:
    """Make a Tally response parseable.

    Five separate real-world failures:
      1. the whole response arriving as UTF-16 (see REQUEST_CONTENT_TYPE);
      2. raw control bytes in ledger names typed from a scanner;
      3. character references like ``&#3;`` that are illegal in XML 1.0;
      4. bytes that are not valid UTF-8 (Tally emits CP1252 in places);
      5. the ``UDF:`` namespace prefix used without ever being declared.
    """
    if looks_utf16(xml_bytes):
        xml_bytes = decode_utf16(xml_bytes)

    xml_bytes = _CHARREF.sub(_strip_bad_charref, xml_bytes)
    xml_bytes = _INVALID_XML_CHARS.sub(b"", xml_bytes)

    try:
        xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        xml_bytes = xml_bytes.decode("utf-8", errors="replace").encode("utf-8")

    if b"UDF:" in xml_bytes and b"xmlns:UDF" not in xml_bytes:
        xml_bytes = xml_bytes.replace(b"<ENVELOPE>", b'<ENVELOPE xmlns:UDF="TallyUDF">', 1)
    return xml_bytes


# --- misc -------------------------------------------------------------------

#: Modern TallyPrime rejects the traditional report name.
REPORT_NAME_ALIASES = {"Profit and Loss A/c": "Profit and Loss"}

#: Tally crashes (c0000005 access violation) when a voucher carries an OBJVIEW
#: attribute. Official Tally sample XML omits it; so do we. Documented here so
#: nobody "helpfully" adds it back.
NEVER_EMIT_ATTRIBUTES = frozenset({"OBJVIEW"})


#: Unit symbols TallyPrime 1.1.7.1 will neither create nor resolve. Creating one
#: answers "DUPLICATE ORIGINAL NAME" (the name is taken internally), and a stock
#: item naming it answers "Unit 'Nos' does not exist!" - so it is simultaneously
#: taken and absent. Measured on 1.1.7.1; "Pcs", "Box", "Dzn" and "Kgs" are fine.
RESERVED_UNIT_NAMES = frozenset({"Nos"})


def unit_is_usable(symbol: str) -> bool:
    """Can a stock item be based on this unit symbol?"""
    return symbol not in RESERVED_UNIT_NAMES


#: TallyPrime in Educational (student) mode accepts vouchers dated only on
#: these days of the month. Everything else is refused at entry.
EDU_ALLOWED_DAYS = (1, 2, 31)


def tally_rate(rate: Decimal, unit: str) -> str:
    """Tally writes a rate as ``2500.00/Nos`` - value, slash, unit.

    A bare number is accepted but loses the unit, and Tally then re-derives the
    rate from amount/quantity, which drifts by a paisa on anything that does
    not divide cleanly.
    """
    return f"{format(rate, 'f')}/{unit}" if unit else format(rate, "f")


def tally_quantity(quantity: Decimal, unit: str) -> str:
    """Quantities carry their unit too: ``20 Nos``."""
    text = format(abs(quantity).normalize(), "f")
    return f"{text} {unit}" if unit else text


def educational_mode_blocks(when: date) -> bool:
    """Would Educational mode refuse a voucher on this date?

    A post on a disallowed date fails with an opaque Tally error, so we check
    before sending rather than interpreting the failure afterwards.
    """
    return when.day not in EDU_ALLOWED_DAYS


def edu_allowed_dates(year: int, month: int) -> list[date]:
    """The dates Educational mode permits in one month.

    The 31st only exists in some months, so it is filtered rather than assumed.
    """
    import calendar

    last = calendar.monthrange(year, month)[1]
    return [date(year, month, day) for day in EDU_ALLOWED_DAYS if day <= last]


def snap_to_edu_date(when: date) -> tuple[date, str]:
    """Move a date to the nearest Educational-mode-legal date.

    Returns ``(snapped_date, warning)``; the warning is empty when nothing
    moved. The caller must surface a non-empty warning - a voucher whose date
    changed silently is a voucher in the wrong period, and at a month boundary
    that is the wrong month's GST return.
    """
    if not educational_mode_blocks(when):
        return when, ""

    candidates = list(edu_allowed_dates(when.year, when.month))
    # The previous and next months matter: the 29th snaps to the 31st of this
    # month if it exists, but in February it belongs to the 1st of March.
    previous_month = (when.replace(day=1) - timedelta(days=1))
    next_month_first = _first_of_next_month(when)
    candidates += edu_allowed_dates(previous_month.year, previous_month.month)
    candidates += edu_allowed_dates(next_month_first.year, next_month_first.month)

    nearest = min(candidates, key=lambda candidate: (abs(candidate - when), candidate))
    return nearest, (
        f"{when} is not enterable in TallyPrime Educational mode "
        f"(only the {', '.join(str(d) for d in EDU_ALLOWED_DAYS)} of a month); "
        f"moved to {nearest}"
    )


def _first_of_next_month(when: date) -> date:
    if when.month == 12:
        return date(when.year + 1, 1, 1)
    return date(when.year, when.month + 1, 1)
