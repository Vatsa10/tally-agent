"""TallyPrime's XML quirks, in one place.

Ported from vendor/tally_mcp_pypi/tally_mcp/xml_parser.py (MIT) plus the sign
conventions from its voucher_builder.py. Every function here exists because
real Tally does something a spec-compliant XML parser refuses to accept.
"""

from __future__ import annotations

import re
from datetime import date
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

    Four separate real-world failures:
      1. raw control bytes in ledger names typed from a scanner;
      2. character references like ``&#3;`` that are illegal in XML 1.0;
      3. bytes that are not valid UTF-8 (Tally emits CP1252 in places);
      4. the ``UDF:`` namespace prefix used without ever being declared.
    """
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


def educational_mode_blocks(when: date) -> bool:
    """Tally in Educational mode only accepts vouchers dated the 1st, 2nd or
    31st of a month. A post on any other date fails with an opaque error, so we
    warn before sending rather than after."""
    return when.day not in (1, 2, 31)
