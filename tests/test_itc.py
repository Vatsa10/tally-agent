"""GSTR-2B against the books: every invoice gets a cause, the rupees at stake,
and what to do. The money is real - over-claimed ITC draws a notice, and
unclaimed ITC lapses - so the matching is tested hardest.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_tools import itc

SUPPLIER = "29AAGCB7383J1Z4"
OTHER = "27AAACZ5521K1Z4"


def portal_doc(*invoices: dict, ctin: str = SUPPLIER, name: str = "Bharat Supplies",
               period: str = "062026") -> dict:
    """A GSTR-2B download in the portal's own shape: ``dt``, ``igst``, ``itcavl``."""
    return {
        "data": {
            "rtnprd": period,
            "docdata": {"b2b": [{"ctin": ctin, "trdnm": name, "inv": list(invoices)}]},
        }
    }


def inv(number: str, taxable: float, igst: float = 0, cgst: float = 0, sgst: float = 0,
        dt: str = "05-06-2026", itcavl: str = "Y", rsn: str = "") -> dict:
    return {
        "inum": number,
        "dt": dt,
        "val": taxable + igst + cgst + sgst,
        "itcavl": itcavl,
        "rsn": rsn,
        "items": [{"txval": taxable, "igst": igst, "cgst": cgst, "sgst": sgst, "cess": 0}],
    }


def bill(number: str, taxable: str, igst: str = "0", party: str = "Bharat Supplies",
         when: str = "2026-06-05") -> dict:
    total = Decimal(taxable) + Decimal(igst)
    return {
        "voucher_number": "7",
        "date": when,
        "party": party,
        "reference": number,
        "taxable_value": taxable,
        "cgst": "0.00",
        "sgst": "0.00",
        "igst": igst,
        "total": format(total, "f"),
    }


GSTINS = {"Bharat Supplies": SUPPLIER, "Zenith Exports": OTHER}


def run(document: dict, rows: list[dict], gstins: dict | None = None) -> itc.Reconciliation:
    period, portal = itc.parse_2b(document)
    return itc.reconcile(period, portal, itc.book_invoices(rows, gstins or GSTINS))


# --- reading the portal's file ------------------------------------------------


def test_the_portals_own_2b_shape_is_read_with_its_tax():
    """The first version read only GSTR-1 style keys, and a real 2B download
    came back with every tax as zero and every date blank."""
    period, invoices = itc.parse_2b(portal_doc(inv("A1", 10000, igst=1800)))

    assert period == "062026"
    assert invoices[0].tax == Decimal("1800.00")
    assert invoices[0].invoice_date == date(2026, 6, 5)


def test_the_gstr1_style_some_tools_export_is_read_too():
    document = {"data": {"rtnprd": "062026", "docdata": {"b2b": [{"ctin": SUPPLIER, "inv": [
        {"inum": "A1", "idt": "2026-06-05", "val": 11800,
         "items": [{"txval": 10000, "iamt": 1800}]}]}]}}}

    _, invoices = itc.parse_2b(document)

    assert invoices[0].tax == Decimal("1800.00")


def test_invoice_numbers_typed_differently_are_the_same_bill():
    assert itc.invoice_key("INV/001") == itc.invoice_key("inv-1") == itc.invoice_key("INV 001")


# --- the causes ---------------------------------------------------------------


def test_a_bill_on_both_sides_is_matched_and_claimable():
    result = run(portal_doc(inv("BS/77", 10000, igst=1800)), [bill("BS-77", "10000", "1800")])

    assert [f.cause for f in result.findings] == [itc.MATCHED]
    assert result.claimable == Decimal("1800.00")
    assert result.at_risk == Decimal("0.00")


def test_two_suppliers_with_the_same_invoice_number_are_not_confused():
    """Small suppliers number their bills 1, 2, 3. Keyed on the number alone,
    Zenith's bill "12" matched Bharat's and both looked fine."""
    document = portal_doc(inv("12", 10000, igst=1800))
    rows = [bill("12", "5000", "900", party="Zenith Exports")]

    causes = {f.cause for f in run(document, rows).findings}

    assert itc.MATCHED not in causes
    assert {itc.NOT_IN_BOOKS, itc.NOT_FILED} <= causes


def test_a_bill_the_supplier_never_filed_puts_its_tax_at_risk():
    result = run(portal_doc(), [bill("BS/90", "4000", "720")])

    finding = result.findings[0]
    assert finding.cause == itc.NOT_FILED
    assert finding.itc_at_risk == Decimal("720.00"), "the tax, not the invoice total"
    assert "deferred" in finding.action


def test_a_bill_in_2b_nobody_keyed_in_is_flagged_without_risk():
    finding = run(portal_doc(inv("BS/91", 4000, igst=720)), []).findings[0]

    assert finding.cause == itc.NOT_IN_BOOKS
    assert finding.itc_at_risk == Decimal("0.00")
    assert finding.ims == "pending", "rejecting a genuine bill loses the credit"


def test_credit_the_portal_bars_is_never_claimable():
    document = portal_doc(inv("BS/92", 3000, igst=540, itcavl="N", rsn="POS rule"))

    finding = run(document, [bill("BS/92", "3000", "540")]).findings[0]

    assert finding.cause == itc.ITC_UNAVAILABLE
    assert "POS rule" in finding.explanation
    assert finding.itc_at_risk == Decimal("540.00")


def test_a_rupee_of_rounding_is_still_a_match():
    result = run(portal_doc(inv("BS/77", 10000, igst=1800.40)), [bill("BS/77", "10000", "1800")])

    assert result.findings[0].cause == itc.MATCHED


def test_a_different_taxable_value_says_by_how_much():
    finding = run(portal_doc(inv("BS/77", 10500, igst=1890)),
                  [bill("BS/77", "10000", "1800")]).findings[0]

    assert finding.cause == itc.VALUE_DIFF
    assert "+500.00" in finding.explanation


def test_the_same_value_at_a_different_rate_is_a_tax_difference():
    finding = run(portal_doc(inv("BS/77", 10000, igst=1200)),
                  [bill("BS/77", "10000", "1800")]).findings[0]

    assert finding.cause == itc.TAX_DIFF
    assert finding.itc_at_risk == Decimal("600.00"), "the excess the books would claim"


def test_the_right_bill_under_the_wrong_gstin_is_named_as_such():
    """It will never match until the ledger is fixed, and it is the least
    obvious of the causes to spot by eye."""
    rows = [bill("BS/77", "10000", "1800")]
    wrong = {"Bharat Supplies": "29AAGCB7383J1Z9"}

    finding = run(portal_doc(inv("BS/77", 10000, igst=1800)), rows, gstins=wrong).findings[0]

    assert finding.cause == itc.GSTIN_MISMATCH
    assert "29AAGCB7383J1Z9" in finding.explanation


def test_the_worst_causes_come_first():
    document = portal_doc(
        inv("OK1", 1000, igst=180),
        inv("BAD", 3000, igst=540, itcavl="N"),
    )
    rows = [bill("OK1", "1000", "180"), bill("BAD", "3000", "540"), bill("LOST", "2000", "360")]

    causes = [f.cause for f in run(document, rows).findings]

    assert causes[0] == itc.ITC_UNAVAILABLE
    assert causes[-1] == itc.MATCHED


# --- follow-ups and IMS -------------------------------------------------------


def test_one_follow_up_per_supplier_listing_every_unfiled_bill():
    result = run(portal_doc(), [bill("BS/90", "4000", "720"), bill("BS/93", "1000", "180")])

    messages = itc.followups(result, firm="Mehta & Co", client="Sharma Textiles")

    assert list(messages) == ["Bharat Supplies"]
    text = messages["Bharat Supplies"]
    assert "BS/90" in text and "BS/93" in text
    assert "Rs 900.00" in text, "the total credit held up"
    assert SUPPLIER in text and "June 2026" in text
    assert text.endswith("Mehta & Co")


def test_ims_suggestions_cover_every_2b_invoice_with_a_reason():
    document = portal_doc(inv("OK1", 1000, igst=180), inv("NEW", 500, igst=90))

    actions = itc.ims_actions(run(document, [bill("OK1", "1000", "180")]))

    by_no = {a["invoice_no"]: a for a in actions}
    assert by_no["OK1"]["suggestion"] == "accept"
    assert by_no["NEW"]["suggestion"] == "pending"
    assert all(a["reason"] for a in actions)


@pytest.mark.parametrize(
    ("invoice_date", "deadline"),
    [(date(2026, 6, 5), date(2027, 11, 30)), (date(2027, 2, 1), date(2027, 11, 30)),
     (date(2027, 4, 1), date(2028, 11, 30))],
)
def test_the_section_16_4_deadline_follows_the_financial_year(invoice_date, deadline):
    assert itc.section_16_4_deadline(invoice_date) == deadline


def test_the_csv_has_a_row_per_finding_in_words():
    result = run(portal_doc(inv("OK1", 1000, igst=180)), [bill("OK1", "1000", "180")])

    text = result.csv()

    assert text.splitlines()[0].startswith("cause,supplier")
    assert "matched" in text
