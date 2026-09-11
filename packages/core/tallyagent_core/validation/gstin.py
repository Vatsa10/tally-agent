"""GSTIN format and checksum.

A GSTIN is 15 chars: 2-digit state code, 10-char PAN, 1 entity number, 'Z',
1 check digit. The check digit is a base-36 weighted-modulus over the first 14
characters — the same scheme the GST portal uses, so a typo'd GSTIN fails here
rather than three weeks later in a GSTR-1 mismatch.
"""

from __future__ import annotations

import re

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_FORMAT = re.compile(r"^[0-3][0-9A-Z][A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def check_digit(first14: str) -> str:
    """Compute the 15th character from the first 14."""
    if len(first14) != 14:
        raise ValueError("need exactly 14 characters")
    total = 0
    for i, ch in enumerate(first14):
        value = ALPHABET.index(ch)
        factor = 1 if i % 2 == 0 else 2
        product = value * factor
        total += product // 36 + product % 36
    return ALPHABET[(36 - total % 36) % 36]


def is_valid(gstin: str | None) -> bool:
    if not gstin:
        return False
    gstin = gstin.strip().upper()
    if len(gstin) != 15 or not _FORMAT.match(gstin):
        return False
    try:
        return check_digit(gstin[:14]) == gstin[14]
    except ValueError:
        return False


def state_code(gstin: str) -> str:
    """The GST state code embedded in a GSTIN — drives IGST vs CGST+SGST."""
    return gstin.strip()[:2]
