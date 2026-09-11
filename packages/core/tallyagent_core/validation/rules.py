"""The deterministic rule set. No model, no network - pure functions of the draft
and a snapshot of the masters. Every write passes through here first.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from tallyagent_core.models import Company, Voucher
from tallyagent_core.models.voucher import PAISA, VALID_GST_RATES
from tallyagent_core.validation import gstin as gstin_mod
from tallyagent_core.validation.report import RuleResult, Severity

# Two invoices from the same party for the same amount inside this many days is
# almost always the same invoice entered twice.
DUPLICATE_WINDOW_DAYS = 30


@dataclass(slots=True)
class PostedVoucher:
    """Minimal shape of an already-posted voucher, for duplicate detection."""

    party_name: str | None
    reference: str
    amount: Decimal
    date: date
    voucher_number: str = ""
    voucher_type: str = ""


@dataclass(slots=True)
class ValidationContext:
    """Everything the rules are allowed to know."""

    company: Company
    known_ledgers: set[str] = field(default_factory=set)
    # alias -> canonical ledger name, learned from past corrections
    ledger_aliases: dict[str, str] = field(default_factory=dict)
    party_state_codes: dict[str, str] = field(default_factory=dict)
    recent_vouchers: list[PostedVoucher] = field(default_factory=list)
    duplicate_window_days: int = DUPLICATE_WINDOW_DAYS
    #: True when the target Tally is a student/Educational install, which only
    #: accepts vouchers dated on EDU_ALLOWED_DAYS.
    edu_mode: bool = False

    def resolve(self, name: str) -> str | None:
        """Exact match, then a learned alias. Never a fuzzy guess - fuzzy
        results are offered as *suggestions*, never silently applied."""
        if name in self.known_ledgers:
            return name
        aliased = self.ledger_aliases.get(name) or self.ledger_aliases.get(name.lower())
        if aliased and aliased in self.known_ledgers:
            return aliased
        return None

    def suggest(self, name: str, n: int = 3) -> list[str]:
        return difflib.get_close_matches(name, sorted(self.known_ledgers), n=n, cutoff=0.6)


def ledgers_exist(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    """Every ledger must already be a master. We never auto-create silently -
    an invented ledger is how a chart of accounts quietly rots."""
    missing: dict[str, list[str]] = {}
    for line in voucher.lines:
        if ctx.resolve(line.ledger_name) is None:
            missing[line.ledger_name] = ctx.suggest(line.ledger_name)
    if not missing:
        return RuleResult("ledgers_exist", True, "all ledgers resolve to masters")
    parts = []
    for name, suggestions in missing.items():
        hint = f" (did you mean: {', '.join(suggestions)}?)" if suggestions else ""
        parts.append(f"{name!r}{hint}")
    return RuleResult(
        "ledgers_exist",
        False,
        "unknown ledger(s): " + "; ".join(parts),
        details={"missing": missing},
    )


def balanced(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    """Debits equal credits to the paisa."""
    dr, cr = voucher.total_debit.quantize(PAISA), voucher.total_credit.quantize(PAISA)
    if dr == cr:
        return RuleResult("balanced", True, f"Dr {dr} = Cr {cr}")
    return RuleResult(
        "balanced",
        False,
        f"debits {dr} != credits {cr} (difference {dr - cr})",
        details={"debit": str(dr), "credit": str(cr)},
    )


def gst_rate_valid(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    if voucher.gst is None:
        return RuleResult("gst_rate_valid", True, "no GST on this voucher")
    if voucher.gst.rate in VALID_GST_RATES:
        return RuleResult("gst_rate_valid", True, f"rate {voucher.gst.rate}% is notified")
    return RuleResult(
        "gst_rate_valid",
        False,
        f"GST rate {voucher.gst.rate} is not a notified rate "
        f"({', '.join(str(r) for r in sorted(VALID_GST_RATES))})",
    )


def gst_split_correct(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    """CGST+SGST for intra-state, IGST for inter-state - decided by the party's
    state vs the company's, not by whatever the invoice image happened to say."""
    gst = voucher.gst
    if gst is None or gst.rate == 0:
        return RuleResult("gst_split_correct", True, "no GST to split")

    party_state = gst.place_of_supply
    if party_state is None and voucher.party_name:
        party_state = ctx.party_state_codes.get(voucher.party_name)
    if party_state is None:
        return RuleResult(
            "gst_split_correct",
            False,
            "cannot determine place of supply: party state unknown and no "
            "place_of_supply on the voucher",
            severity=Severity.WARNING,
        )

    interstate = party_state.zfill(2) != ctx.company.state_code
    if interstate and (gst.cgst or gst.sgst):
        return RuleResult(
            "gst_split_correct",
            False,
            f"inter-state supply ({ctx.company.state_code} -> {party_state}) "
            "must use IGST, not CGST+SGST",
        )
    if not interstate and gst.igst:
        return RuleResult(
            "gst_split_correct",
            False,
            f"intra-state supply (both {ctx.company.state_code}) must use "
            "CGST+SGST, not IGST",
        )
    # Halving an odd tax amount legitimately leaves one paisa on one side.
    if not interstate and abs(gst.cgst - gst.sgst) > PAISA:
        return RuleResult(
            "gst_split_correct",
            False,
            f"CGST {gst.cgst} and SGST {gst.sgst} must be equal",
        )

    expected = (gst.taxable_value * gst.rate / Decimal("100")).quantize(PAISA)
    actual = (gst.total_tax - gst.cess).quantize(PAISA)
    # One paisa of rounding either way is normal on a split tax.
    if abs(actual - expected) > PAISA:
        return RuleResult(
            "gst_split_correct",
            False,
            f"tax {actual} does not match {gst.rate}% of taxable value "
            f"{gst.taxable_value} (expected {expected})",
        )
    kind = "IGST" if interstate else "CGST+SGST"
    return RuleResult("gst_split_correct", True, f"{kind} {actual} on {gst.taxable_value}")


def gstin_valid(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    """The company GSTIN must checksum. A typo here corrupts every return."""
    if not ctx.company.gstin:
        return RuleResult("gstin_valid", True, "no company GSTIN configured")
    if gstin_mod.is_valid(ctx.company.gstin):
        return RuleResult("gstin_valid", True, "company GSTIN checksum passes")
    return RuleResult(
        "gstin_valid", False, f"invalid company GSTIN {ctx.company.gstin!r}"
    )


def period_open(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    period = ctx.company.period
    if period is None:
        return RuleResult(
            "period_open",
            False,
            "no financial period configured; cannot check the voucher date",
            severity=Severity.WARNING,
        )
    if period.is_locked(voucher.date):
        return RuleResult(
            "period_open",
            False,
            f"{voucher.date} falls in a closed period "
            f"(locked before {period.locked_before})",
        )
    if not period.contains(voucher.date):
        return RuleResult(
            "period_open",
            False,
            f"{voucher.date} is outside the financial year "
            f"{period.start} to {period.end}",
        )
    return RuleResult("period_open", True, f"{voucher.date} is in an open period")


#: Without an invoice number, two identical payments this many days apart are
#: treated as the same one. Tighter than the reference-matched window, because
#: a genuine second payment of the same amount to the same party does happen -
#: just rarely on the same day.
UNREFERENCED_WINDOW_DAYS = 1


def not_duplicate(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    """Same party and amount, matched on the invoice reference where there is
    one and on the date where there is not.

    Receipts and payments often carry no reference, and matching on reference
    alone let the same payment be posted twice - found live. An unreferenced
    voucher therefore matches on party, amount, type and a much tighter date
    window instead of being waved through.
    """
    window = timedelta(days=ctx.duplicate_window_days)
    tight = timedelta(days=UNREFERENCED_WINDOW_DAYS)
    amount = voucher.amount

    for posted in ctx.recent_vouchers:
        if posted.party_name != voucher.party_name:
            continue
        if posted.amount.quantize(PAISA) != amount:
            continue

        if voucher.reference and posted.reference:
            if posted.reference != voucher.reference:
                continue
            if abs(posted.date - voucher.date) > window:
                continue
            why = f"reference {posted.reference!r} and amount {amount}"
        else:
            # No reference on one side or the other: fall back to date.
            if abs(posted.date - voucher.date) > tight:
                continue
            if posted.voucher_type and posted.voucher_type != voucher.voucher_type.value:
                continue
            why = (
                f"amount {amount} on the same date, and neither carries an "
                "invoice reference to tell them apart"
            )

        number = posted.voucher_number or "(unnumbered)"
        return RuleResult(
            "not_duplicate",
            False,
            f"looks like a duplicate of voucher {number} dated {posted.date}: "
            f"same party, {why}",
            details={
                "voucher_number": posted.voucher_number,
                "date": posted.date.isoformat(),
            },
        )
    return RuleResult("not_duplicate", True, "no matching voucher in the window")


#: Days of the month a TallyPrime Educational (student) install will accept.
#: Duplicated from tallyagent_tally.xml.quirks rather than imported: core must
#: not depend on the adapter. The two are asserted equal in the tests.
EDU_ALLOWED_DAYS = (1, 2, 31)


def edu_date_allowed(voucher: Voucher, ctx: ValidationContext) -> RuleResult:
    """Educational mode only accepts the 1st, 2nd and 31st of a month.

    Checked here so the failure is a readable report line naming the dates that
    would work, rather than an opaque Tally rejection after the envelope has
    gone. Inert when the target is a licensed install.
    """
    if not ctx.edu_mode:
        return RuleResult("edu_date_allowed", True, "not an Educational install")
    if voucher.date.day in EDU_ALLOWED_DAYS:
        return RuleResult(
            "edu_date_allowed", True, f"{voucher.date} is enterable in Educational mode"
        )
    allowed = ", ".join(str(day) for day in EDU_ALLOWED_DAYS)
    return RuleResult(
        "edu_date_allowed",
        False,
        f"TallyPrime Educational mode will not accept a voucher dated "
        f"{voucher.date}; only the {allowed} of a month are enterable",
        details={"allowed_days": list(EDU_ALLOWED_DAYS), "date": voucher.date.isoformat()},
    )


#: Run in this order; the report reads top-to-bottom in the approval UI.
ALL_RULES = (
    ledgers_exist,
    balanced,
    gst_rate_valid,
    gst_split_correct,
    gstin_valid,
    period_open,
    edu_date_allowed,
    not_duplicate,
)
