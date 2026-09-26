"""Scoring the bill reader.

The scoring has to be harder to fool than the reader, or the number it prints is
worse than no number at all. Everything here is the comparison, which is pure;
reading an actual scan is what the run against the pile does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_bills import Report, compare  # noqa: E402

from tallyagent_tools import bills  # noqa: E402

TRUTH = {
    "vendor": "Bharat Supplies",
    "invoice_no": "BS/2026/002",
    "invoice_date": "2026-06-01",
    "taxable_value": 7500.0,
    "cgst": 0,
    "sgst": 0,
    "igst": 1350.0,
    "total": 8850.0,
}


def test_a_correct_reading_has_nothing_wrong():
    assert compare(dict(TRUTH), TRUTH) == {}


def test_a_misread_amount_is_caught():
    wrong = compare({**TRUTH, "total": 8350.0}, TRUTH)

    assert "total" in wrong
    assert wrong["total"] == ("8350.0", "8850.0")


def test_rounding_noise_in_a_truth_file_is_not_a_misread():
    """Truth files are JSON, so money arrives as a float."""
    assert compare({**TRUTH, "total": 8850.000000001}, TRUTH) == {}


def test_a_rupee_out_is_a_misread():
    assert "total" in compare({**TRUTH, "total": 8851.0}, TRUTH)


def test_the_same_date_written_differently_is_the_same_date():
    assert compare({**TRUTH, "invoice_date": "2026-06-01T00:00:00"}, TRUTH) == {}


def test_the_wrong_date_is_caught():
    assert "invoice_date" in compare({**TRUTH, "invoice_date": "2026-06-02"}, TRUTH)


def test_spacing_and_case_in_a_vendor_name_are_not_a_misread():
    """"BHARAT  SUPPLIES" off an OCR pass is the same supplier."""
    assert compare({**TRUTH, "vendor": "BHARAT  SUPPLIES"}, TRUTH) == {}


def test_a_different_vendor_is_caught():
    assert "vendor" in compare({**TRUTH, "vendor": "Bharat Supply Co"}, TRUTH)


def test_a_vendor_with_the_spaces_lost_is_not_a_miss():
    """What is being measured is what a partner would have to catch, and the
    product resolves "BharatSupplies" to the ledger by the same rule."""
    assert compare({**TRUTH, "vendor": "BharatSupplies"}, TRUTH) == {}


def test_a_nil_tax_read_as_nil_is_correct():
    """On an inter-state bill CGST and SGST are genuinely nil, and reading them
    as nil must not count as a miss."""
    assert compare(dict(TRUTH), TRUTH) == {}
    assert "cgst" in compare({**TRUTH, "cgst": 675.0}, TRUTH)


def test_a_field_the_original_does_not_show_is_not_scored():
    """Published samples come with the GSTIN and invoice number blacked out."""
    redacted = {**TRUTH, "invoice_no": ""}

    assert compare({**TRUTH, "invoice_no": "anything"}, redacted) == {}


def test_a_missing_amount_is_a_misread_not_a_pass():
    assert "taxable_value" in compare({**TRUTH, "taxable_value": None}, TRUTH)


# --- what the report says -----------------------------------------------------


def _report() -> Report:
    from eval_bills import BillScore

    report = Report(label="test", folder="x")
    report.bills = [
        BillScore(name="a.png", status=bills.QUEUED),
        BillScore(name="b.png", status=bills.QUEUED, wrong={"total": ("1", "2")}),
        BillScore(name="c.png", status=bills.ATTENTION, error="no text could be read"),
    ]
    return report


def test_the_number_that_matters_is_drafted_and_wrong():
    """A bill on the attention list is the reader working, not failing; counting
    it as an error would reward a reader that guesses."""
    report = _report()

    assert [b.name for b in report.queued_wrong] == ["b.png"]
    assert [b.name for b in report.attention] == ["c.png"]


def test_the_report_says_which_bill_and_which_field():
    text = _report().markdown()

    assert "**1 drafted with something wrong**" in text
    assert "`b.png` total: read '1', should be '2'" in text
    assert "`c.png`: no text could be read" in text


def test_drafting_from_an_unreadable_scan_counts_against_it():
    """Inventing figures for a document nobody can read is worse than refusing
    it, so it is counted with the misreads rather than praised as a draft."""
    from eval_bills import BillScore

    report = Report(label="t", folder="x")
    report.bills = [
        BillScore(name="junk.png", status=bills.QUEUED, unreadable=True),
    ]

    assert [b.name for b in report.queued_wrong] == ["junk.png"]


def test_refusing_an_unreadable_scan_is_not_counted_against_it():
    from eval_bills import BillScore

    report = Report(label="t", folder="x")
    report.bills = [
        BillScore(name="junk.png", status=bills.ATTENTION, error="refused, correctly"),
    ]

    assert report.queued_wrong == []
    assert [b.name for b in report.attention] == ["junk.png"]
