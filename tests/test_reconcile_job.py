"""The monthly GSTR-2B job, end to end against the fake Tally.

One client, two months. In June a supplier has not filed one bill, another bill
is in 2B that nobody keyed in, and one is from a supplier the books do not know.
In July the unfiled bill turns up - and the credit has to be called claimable,
not forgotten.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from tallyagent_approvals.db import make_engine
from tallyagent_daemon import clients as clients_mod
from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_daemon.firm.inbox import Inbox
from tallyagent_daemon.firm.jobs import EXCEPTION, QUEUED
from tallyagent_daemon.firm.reconcile import Reconcile2BJob, find_2b, nearest_slab
from tallyagent_daemon.firm.runner import FirmRunner
from tallyagent_tally.fake_server import seeded_demo
from tallyagent_tools import vouchers

BHARAT = "29AAGCB7383J1Z4"


def two_b(
    period: str,
    invoices: list[dict],
    supplier: tuple[str, str] = (BHARAT, "Bharat Supplies"),
    extra: list[dict] | None = None,
) -> dict:
    b2b = [{"ctin": supplier[0], "trdnm": supplier[1], "inv": invoices}] + (extra or [])
    return {"data": {"rtnprd": period, "docdata": {"b2b": b2b}}}


def portal_inv(number: str, taxable: float, igst: float, dt: str) -> dict:
    return {"inum": number, "dt": dt, "val": taxable + igst, "itcavl": "Y",
            "items": [{"txval": taxable, "igst": igst, "cgst": 0, "sgst": 0, "cess": 0}]}


@pytest.fixture
def client_setup(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "2b"
    folder.mkdir()
    register = clients_mod.parse(
        {
            "storage": {"data_dir": str(tmp_path / "data")},
            "client": [{"slug": "sharma", "company": "Sharma Textiles",
                        "gstr2b_dir": str(folder)}],
        }
    )
    base = config_mod.from_dict({"storage": {"firm_db_path": str(tmp_path / "firm.db")}})
    tally = seeded_demo("Sharma Textiles")
    wired = wiring.build(
        clients_mod.apply(base, register.clients[0], register.data_dir),
        transport=tally.transport,
    )
    runner = FirmRunner(register, lambda c: wired,
                        Inbox(make_engine(str(tmp_path / "firm.db"))), jobs=[Reconcile2BJob()])
    return wired, runner, folder


async def book_purchase(wired, number: str, taxable: str) -> None:  # type: ignore[no-untyped-def]
    """A purchase entered and approved, so it is in the register."""
    result = await vouchers.create_purchase_voucher(
        wired.services.tools, party_name="Bharat Supplies", taxable_value=taxable,
        gst_rate="18", voucher_date=date(2026, 6, 2), reference=number,
    )
    await wired.services.queue.approve(result.data["ticket"], "R. Mehta")


async def test_without_the_2b_download_it_says_exactly_where_to_put_it(client_setup):
    _, runner, folder = client_setup

    report = await runner.run(date(2026, 7, 15))

    outcome = report.outcomes[0]
    assert outcome.kind == EXCEPTION
    assert "GSTR-2B for 2026-06 has not been downloaded" in outcome.title
    assert str(folder) in outcome.detail
    assert outcome.due == date(2026, 7, 20), "3B is due on the 20th"


async def test_a_months_reconciliation_end_to_end(client_setup, tmp_path):
    wired, runner, folder = client_setup
    await book_purchase(wired, "BS/77", "10000")   # in 2B: matched
    await book_purchase(wired, "BS/90", "4000")    # not in 2B: supplier has not filed
    (folder / "june.json").write_text(json.dumps(two_b(
        "062026",
        [portal_inv("BS/77", 10000, 1800, "02-06-2026"),
         portal_inv("BS/91", 2000, 360, "03-06-2026")],          # nobody keyed it in
        extra=[{"ctin": "27AAACZ5521K1Z4", "trdnm": "Unknown Traders",
                "inv": [portal_inv("UT/1", 500, 90, "04-06-2026")]}],
    )), encoding="utf-8")

    report = await runner.run(date(2026, 7, 15))
    by_title = {o.title: o for o in report.outcomes}

    summary = next(o for o in report.outcomes if o.title.startswith("GSTR-2B 062026"))
    assert "Claimable Rs 1,800.00" in summary.title

    chase = by_title["Bharat Supplies has not filed 1 invoice(s)"]
    assert chase.kind == EXCEPTION
    assert chase.amount_at_risk == Decimal("720.00"), "the tax on BS/90, not its total"

    drafted = next(o for o in report.outcomes if "Drafted missing bill" in o.title)
    assert drafted.kind == QUEUED and drafted.ticket, "a draft for a person to approve"
    assert wired.services.queue.get(drafted.ticket).status == "pending"

    unknown = next(o for o in report.outcomes if "Unknown Traders" in o.title)
    assert unknown.kind == EXCEPTION
    assert "not a ledger here" in unknown.detail, "never invents a supplier"

    ims = next(o for o in report.outcomes if o.title.startswith("IMS suggestions"))
    assert "1 to accept" in ims.title

    out = tmp_path / "reports" / "reco" / "Sharma Textiles" / "2026-06"
    assert (out / "reco.csv").exists() and (out / "ims.csv").exists()
    followup = (out / "followups" / "Bharat Supplies.txt").read_text(encoding="utf-8")
    assert "BS/90" in followup and "Rs 720.00" in followup


async def test_a_deferred_credit_is_called_claimable_the_month_it_appears(client_setup):
    wired, runner, folder = client_setup
    await book_purchase(wired, "BS/90", "4000")
    (folder / "june.json").write_text(json.dumps(two_b("062026", [])), encoding="utf-8")
    await runner.run(date(2026, 7, 15))

    (folder / "july.json").write_text(json.dumps(two_b(
        "072026", [portal_inv("BS/90", 4000, 720, "02-06-2026")]
    )), encoding="utf-8")
    report = await runner.run(date(2026, 8, 15))

    titles = [o.title for o in report.outcomes]
    assert "Rs 720.00 of deferred ITC is now claimable in 2026-07" in titles


async def test_running_the_same_month_twice_does_not_defer_the_credit_twice(client_setup):
    from tallyagent_approvals.deferred_itc import DeferredItcLedger

    wired, runner, folder = client_setup
    await book_purchase(wired, "BS/90", "4000")
    (folder / "june.json").write_text(json.dumps(two_b("062026", [])), encoding="utf-8")

    await runner.run(date(2026, 7, 15))
    await runner.run(date(2026, 7, 16))

    assert len(DeferredItcLedger(wired.services.audit.engine).open("Sharma Textiles")) == 1


def test_a_2b_file_is_found_by_the_period_inside_it_not_its_name(tmp_path):
    (tmp_path / "anything.json").write_text(json.dumps(two_b("052026", [])), encoding="utf-8")
    (tmp_path / "GSTR2B_june_final_FINAL.json").write_text(
        json.dumps(two_b("062026", [])), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    found = find_2b(tmp_path, "2026-06")

    assert found is not None and found[0].name == "GSTR2B_june_final_FINAL.json"
    assert find_2b(tmp_path, "2026-04") is None
    assert find_2b(tmp_path / "missing", "2026-06") is None


@pytest.mark.parametrize(("taxable", "tax", "slab"), [
    ("10000", "1800", "18"), ("10000", "500", "5"), ("10000", "1190", "12"), ("1000", "0", "0"),
])
def test_a_rate_is_recovered_from_the_tax(taxable, tax, slab):
    assert nearest_slab(Decimal(taxable), Decimal(tax)) == Decimal(slab)


def test_it_runs_between_the_14th_and_the_end_of_the_month():
    job = Reconcile2BJob()

    assert not job.due(None, date(2026, 7, 13))
    assert job.due(None, date(2026, 7, 14))
    assert job.due(None, date(2026, 7, 31))

