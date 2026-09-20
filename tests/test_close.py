"""The month-end close pack.

The checks are pure functions over rows, so what they say about a set of books
is tested here without a Tally. What is not tested is whether Tally's numbers
are right - that is what the live run is for.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from tallyagent_tools import close
from tallyagent_tools.close import (
    ClosePack,
    Finding,
    bank_findings,
    duplicate_vouchers,
    gst_findings,
    month_dates,
    negative_cash,
    negative_stock,
    overdue_receivables,
    write_pack,
)

# --- the month ---------------------------------------------------------------


def test_a_month_runs_to_its_real_last_day():
    assert month_dates("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_dates("2024-02")[1] == date(2024, 2, 29), "leap year"
    assert month_dates("2026-12")[1] == date(2026, 12, 31)


# --- the checks --------------------------------------------------------------


def test_cash_below_zero_is_the_loudest_finding():
    """A negative till is impossible, so it is always an entry error."""
    findings = negative_cash(
        {"Cash": Decimal("-4200.00")}, {"Cash": "Cash-in-Hand"}
    )

    assert [f.severity for f in findings] == ["high"]
    assert findings[0].amount == Decimal("-4200.00")
    assert "receipt" in findings[0].fix


def test_an_overdrawn_bank_is_a_question_not_an_error():
    """Overdrafts are a normal facility; a negative till is not."""
    findings = negative_cash(
        {"Bank - HDFC": Decimal("-100.00")}, {"Bank - HDFC": "Bank Accounts"}
    )

    assert [f.severity for f in findings] == ["medium"]


def test_healthy_balances_produce_nothing():
    assert negative_cash({"Cash": Decimal("500")}, {"Cash": "Cash-in-Hand"}) == []


def test_negative_stock_means_a_purchase_was_never_entered():
    findings = negative_stock({"Widget": Decimal("-2"), "Gadget": Decimal("5")})

    assert len(findings) == 1
    assert "Widget" in findings[0].detail


def test_ageing_is_reported_per_party_not_per_bill():
    """A partner asks "who owes us", not "which of the nine bills"."""
    findings = overdue_receivables(
        [
            {"party": "Acme", "amount": "1000", "overdue_days": 120},
            {"party": "Acme", "amount": "500", "overdue_days": 95},
            {"party": "Zenith", "amount": "900", "overdue_days": 10},
        ]
    )

    assert len(findings) == 1
    assert findings[0].amount == Decimal("1500")


def test_the_same_bill_entered_twice_is_raised_as_a_question():
    findings = duplicate_vouchers(
        [
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "7552.00", "number": "12"},
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "7552.00", "number": "13"},
        ]
    )

    assert len(findings) == 1
    assert "12, 13" in findings[0].detail
    assert findings[0].severity == "medium", "not proof, so not a correction"


def test_the_lines_of_one_voucher_are_not_a_duplicate_of_each_other():
    """A day book lists a voucher once per ledger line, all with one number."""
    findings = duplicate_vouchers(
        [
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "7552.00", "number": "12"},
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "7552.00", "number": "12"},
        ]
    )

    assert findings == []


def test_two_different_amounts_on_a_day_are_not_a_duplicate():
    findings = duplicate_vouchers(
        [
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "100.00", "number": "12"},
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "200.00", "number": "13"},
        ]
    )

    assert findings == []


def test_purchases_missing_from_2b_are_reported_as_credit_at_risk():
    """The row count is not the point; the money that cannot be claimed is."""
    findings = gst_findings(
        {"missing_in_2b": 2},
        [
            {"status": "missing_in_2b", "books_total": "11800.00"},
            {"status": "missing_in_2b", "books_total": "5900.00"},
            {"status": "matched", "books_total": "1000.00"},
        ],
    )

    assert findings[0].kind == "itc_at_risk"
    assert findings[0].amount == Decimal("17700.00")
    assert findings[0].severity == "high"


def test_a_clean_2b_produces_no_findings():
    assert gst_findings({"matched": 12}, []) == []


def test_statement_lines_with_no_voucher_outrank_uncleared_cheques():
    findings = bank_findings(
        {
            "unmatched_statement": [{"amount": "-2500.00"}],
            "unmatched_ledger": [{"amount": "900.00"}],
        }
    )

    assert [f.severity for f in findings] == ["high", "medium"]
    assert findings[0].amount == Decimal("2500.00")


# --- the pack ----------------------------------------------------------------


def _pack(**kwargs) -> ClosePack:  # type: ignore[no-untyped-def]
    return ClosePack(
        company="TA-Demo Traders",
        month="2026-06",
        from_date=date(2026, 6, 1),
        to_date=date(2026, 6, 30),
        **kwargs,
    )


def test_the_worst_finding_is_read_first():
    pack = _pack(
        findings=[
            Finding("a", "medium", "medium one", Decimal("10")),
            Finding("b", "high", "small but serious", Decimal("1")),
            Finding("c", "high", "large and serious", Decimal("999")),
        ]
    )

    assert [f.detail for f in pack.sorted_findings()] == [
        "large and serious",
        "small but serious",
        "medium one",
    ]


def test_a_clean_month_says_so_rather_than_showing_an_empty_table():
    assert "Nothing. The checks above all passed." in _pack().markdown()


def test_the_pack_says_it_posted_nothing():
    """Somebody will forward this to a client. It has to be unambiguous."""
    assert "has been posted" in _pack().markdown()


def test_the_pack_and_its_workings_are_written_together(tmp_path):  # type: ignore[no-untyped-def]
    pack = _pack(
        findings=[Finding("x", "high", "something", Decimal("1"))],
        attachments={"2026-06-gstr2b.csv": "status\nmatched\n"},
    )

    path = write_pack(pack, tmp_path)

    assert path.read_text(encoding="utf-8").startswith("# Month-end close")
    assert (path.parent / "2026-06-gstr2b.csv").exists()


def test_rerunning_a_month_overwrites_rather_than_piling_up(tmp_path):  # type: ignore[no-untyped-def]
    write_pack(_pack(), tmp_path)
    write_pack(_pack(findings=[Finding("x", "high", "new", Decimal("1"))]), tmp_path)

    folder = tmp_path / "TA-Demo Traders" / "2026-06"
    assert [p.name for p in folder.iterdir()] == ["close.md"]
    assert "new" in (folder / "close.md").read_text(encoding="utf-8")


# --- the whole run, against a fake Tally -------------------------------------


class FakeResult:
    def __init__(self, message: str = "", data=None) -> None:  # type: ignore[no-untyped-def]
        self.message = message
        self.data = data


class FakeCompany:
    name = "TA-Demo Traders"


class FakeCtx:
    company = FakeCompany()


async def test_a_close_without_the_files_says_what_it_could_not_check(
    tmp_path, monkeypatch
):  # type: ignore[no-untyped-def]
    """Running on the 1st, before the bank or the portal has sent anything, is
    the normal case - and silently reporting a bank balance nobody ticked is
    how a pack becomes misleading."""
    _stub_tally(monkeypatch)

    result = await close.month_end_close(
        FakeCtx(), "2026-06", out_dir=str(tmp_path)  # type: ignore[arg-type]
    )

    assert "Not run" in result.data["sections"]["Bank reconciliation"]
    assert "Not run" in result.data["sections"]["GSTR-2B"]
    assert result.data["counts"]["high"] == 1, "the negative cash still shows"
    assert "Nothing was posted" in result.message


async def test_a_close_with_both_files_folds_them_into_one_pack(
    tmp_path, monkeypatch
):  # type: ignore[no-untyped-def]
    _stub_tally(monkeypatch)

    statement = tmp_path / "statement.csv"
    statement.write_text("date,narration,amount\n2026-06-03,NEFT ACME,2500\n", "utf-8")
    portal = tmp_path / "2b.json"
    portal.write_text(json.dumps({"docdata": {"b2b": []}}), "utf-8")

    async def fake_bank_reco(ctx, rows, **kwargs):  # type: ignore[no-untyped-def]
        assert rows[0]["narration"] == "NEFT ACME", "the CSV was actually read"
        return FakeResult(
            "1 matched",
            {
                "unmatched_statement": [{"amount": "2500.00"}],
                "unmatched_ledger": [],
                "proposals": [{"tool": "create_receipt"}],
            },
        )

    async def fake_2b(ctx, portal_json, **kwargs):  # type: ignore[no-untyped-def]
        return FakeResult(
            "GSTR-2B reconciliation",
            {
                "counts": {"missing_in_2b": 1},
                "rows": [{"status": "missing_in_2b", "books_total": "11800.00"}],
                "csv": "status\nmissing_in_2b\n",
            },
        )

    from tallyagent_tools import reconcile

    monkeypatch.setattr(reconcile, "bank_reco", fake_bank_reco)
    monkeypatch.setattr(reconcile, "gstr2b_vs_purchase_register", fake_2b)

    result = await close.month_end_close(
        FakeCtx(),  # type: ignore[arg-type]
        "2026-06",
        bank_statement=str(statement),
        gstr2b=str(portal),
        out_dir=str(tmp_path),
    )

    kinds = {f["kind"] for f in result.data["findings"]}
    assert {"negative_cash", "bank_not_in_books", "itc_at_risk"} <= kinds
    folder = tmp_path / "TA-Demo Traders" / "2026-06"
    assert (folder / "2026-06-gstr2b.csv").exists()
    assert (folder / "2026-06-bank-proposals.json").exists()


def _stub_tally(monkeypatch):  # type: ignore[no-untyped-def]
    """A company with one problem in it: the till is negative."""
    from tallyagent_tools import reports, stock

    async def balances(ctx):  # type: ignore[no-untyped-def]
        return {"Cash": Decimal("-100.00")}, {"Cash": "Cash-in-Hand"}

    async def cash_position(ctx):  # type: ignore[no-untyped-def]
        return FakeResult("Cash and bank: -100.00 across 1 account(s).", {})

    async def on_hand(ctx, as_on=None):  # type: ignore[no-untyped-def]
        return {"Widget": Decimal("4")}

    async def receivables(ctx, as_on=None, party=""):  # type: ignore[no-untyped-def]
        return FakeResult("Total receivable: 0", {"bills": []})

    async def day_book(ctx, from_date=None, to_date=None, **kwargs):  # type: ignore[no-untyped-def]
        return FakeResult("", [])

    monkeypatch.setattr(reports, "_balances", balances)
    monkeypatch.setattr(reports, "cash_position", cash_position)
    monkeypatch.setattr(reports, "outstanding_receivables", receivables)
    monkeypatch.setattr(reports, "day_book", day_book)
    monkeypatch.setattr(stock, "on_hand", on_hand)


def test_a_multi_line_voucher_does_not_report_itself_as_a_duplicate():
    """A day book row per ledger line is the shape Tally actually returns, and
    the first version compared a sale's debit line against its own two credits
    and cried duplicate three times over."""
    findings = duplicate_vouchers(
        [
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "7552.00", "number": "12"},
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "-6400.00", "number": "12"},
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "-1152.00", "number": "12"},
        ]
    )

    assert findings == []


def test_a_sales_14_and_a_journal_14_are_not_the_same_voucher():
    """Tally numbers vouchers per type, so the numbers collide. Merged, the
    pack reported a journal whose amount was two vouchers added together."""
    findings = duplicate_vouchers(
        [
            {"date": "2026-06-02", "party": "Acme", "type": "Sales",
             "amount": "7552.00", "number": "14"},
            {"date": "2026-06-01", "party": "Cash", "type": "Journal",
             "amount": "100.00", "number": "14"},
        ]
    )

    assert findings == []
