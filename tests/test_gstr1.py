"""The GSTR-1 JSON a CA uploads to the GST portal.

What is being protected: a file the portal would reject is never written. The
shape is pinned by a golden month, each rejection reason has its own test, and
the whole export runs once against the fake Tally so the GSTINs really come
from the ledger masters.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tallyagent_core.idempotency import make_key
from tallyagent_core.models import Voucher, VoucherLine, VoucherType
from tallyagent_tools import registry
from tallyagent_tools.base import ToolContext
from tallyagent_tools.gstr1 import build_gstr1, gstr1_export, validate

HOME = "27AAPFU0939F1ZV"  # Maharashtra, the supplier
MH_BUYER = "27AAGCB7383J1Z8"
KA_BUYER = "29AAGCB7383J1Z4"


def _row(number, party, taxable, cgst="0", sgst="0", igst="0", **extra):  # type: ignore[no-untyped-def]
    total = Decimal(taxable) + Decimal(cgst) + Decimal(sgst) + Decimal(igst)
    return {
        "voucher_number": number,
        "reference": number,
        "date": "2026-06-15",
        "party": party,
        "taxable_value": taxable,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "total": str(total),
        **extra,
    }


def _june() -> list[dict[str, str]]:
    return [
        _row("INV-001", "Acme", "10000.00", "900.00", "900.00", gstin=MH_BUYER),
        _row("INV-002", "Bharat", "5000.00", igst="600.00", gstin=KA_BUYER),
        _row("INV-003", "Walk-in", "1000.00", "25.00", "25.00"),
        _row("INV-004", "Walk-in", "2000.00", "50.00", "50.00"),
    ]


# --- the golden month --------------------------------------------------------


def test_a_month_of_b2b_and_b2c_matches_the_portal_shape():
    assert validate(_june(), HOME) == []

    assert build_gstr1(_june(), HOME, "062026") == {
        "gstin": HOME,
        "fp": "062026",
        "b2b": [
            {
                "ctin": MH_BUYER,
                "inv": [
                    {
                        "inum": "INV-001",
                        "idt": "15-06-2026",
                        "val": 11800.0,
                        "pos": "27",
                        "rchrg": "N",
                        "inv_typ": "R",
                        "itms": [
                            {
                                "num": 1,
                                "itm_det": {
                                    "rt": 18.0,
                                    "txval": 10000.0,
                                    "iamt": 0.0,
                                    "camt": 900.0,
                                    "samt": 900.0,
                                    "csamt": 0.0,
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "ctin": KA_BUYER,
                "inv": [
                    {
                        "inum": "INV-002",
                        "idt": "15-06-2026",
                        "val": 5600.0,
                        "pos": "29",
                        "rchrg": "N",
                        "inv_typ": "R",
                        "itms": [
                            {
                                "num": 1,
                                "itm_det": {
                                    "rt": 12.0,
                                    "txval": 5000.0,
                                    "iamt": 600.0,
                                    "camt": 0.0,
                                    "samt": 0.0,
                                    "csamt": 0.0,
                                },
                            }
                        ],
                    }
                ],
            },
        ],
        "b2cs": [
            {
                "sply_ty": "INTRA",
                "pos": "27",
                "typ": "OE",
                "rt": 5.0,
                "txval": 3000.0,
                "iamt": 0.0,
                "camt": 75.0,
                "samt": 75.0,
                "csamt": 0.0,
            }
        ],
    }


def test_the_hsn_summary_appears_only_when_every_supply_has_a_code():
    rows = [{**r, "hsn": "8471"} for r in _june()]
    with_codes = build_gstr1(rows, HOME, "062026")
    assert [line["hsn_sc"] for line in with_codes["hsn"]["data"]] == ["8471"] * 3

    rows[0]["hsn"] = ""
    assert "hsn" not in build_gstr1(rows, HOME, "062026")


def test_unregistered_buyers_in_other_states_are_inter_state_b2cs():
    rows = [_row("INV-9", "Walk-in", "1000.00", igst="180.00", pos="29")]
    payload = build_gstr1(rows, HOME, "062026")
    assert payload["b2b"] == []
    assert payload["b2cs"][0]["sply_ty"] == "INTER"
    assert payload["b2cs"][0]["pos"] == "29"


def test_a_large_inter_state_sale_to_an_unregistered_buyer_is_b2cl_not_b2cs():
    rows = [
        _row("INV-L", "Walk-in", "200000.00", igst="36000.00", pos="29"),
        _row("INV-S", "Walk-in", "1000.00", igst="180.00", pos="29"),
    ]
    payload = build_gstr1(rows, HOME, "062026")
    assert payload["b2cl"] == [
        {
            "pos": "29",
            "inv": [
                {
                    "inum": "INV-L",
                    "idt": "15-06-2026",
                    "val": 236000.0,
                    "itms": [
                        {
                            "num": 1,
                            "itm_det": {
                                "rt": 18.0,
                                "txval": 200000.0,
                                "iamt": 36000.0,
                                "csamt": 0.0,
                            },
                        }
                    ],
                }
            ],
        }
    ]
    assert [row["txval"] for row in payload["b2cs"]] == [1000.0]


def test_a_large_intra_state_sale_to_an_unregistered_buyer_stays_in_b2cs():
    rows = [_row("INV-L", "Walk-in", "200000.00", "18000.00", "18000.00")]
    payload = build_gstr1(rows, HOME, "062026")
    assert "b2cl" not in payload
    assert payload["b2cs"][0]["txval"] == 200000.0


# --- what the portal would reject --------------------------------------------


def test_the_same_invoice_number_twice_in_a_month_is_a_problem():
    rows = [
        _row("INV-1", "Walk-in", "1000.00", "25.00", "25.00"),
        _row("inv-1", "Walk-in", "1000.00", "25.00", "25.00"),
    ]
    assert any("used twice" in p for p in validate(rows, HOME))


def test_a_company_gstin_with_a_bad_checksum_is_a_problem():
    problems = validate(_june(), "27AAPFU0939F1ZA")
    assert any("company GSTIN" in p for p in problems)


def test_a_buyer_gstin_with_a_bad_checksum_is_a_problem():
    rows = [_row("INV-001", "Acme", "10000.00", "900.00", "900.00", gstin="27AAGCB7383J1Z9")]
    assert any("checksum" in p for p in validate(rows, HOME))


def test_an_invoice_number_longer_than_sixteen_characters_is_a_problem():
    rows = [_row("INV-2026-06-000001", "Walk-in", "1000.00", "90.00", "90.00")]
    assert any("1-16" in p for p in validate(rows, HOME))


def test_an_invoice_number_with_a_character_the_portal_refuses_is_a_problem():
    rows = [_row("INV 001#", "Walk-in", "1000.00", "90.00", "90.00")]
    assert any("1-16" in p for p in validate(rows, HOME))


def test_tax_more_than_a_rupee_off_the_rate_is_a_problem():
    rows = [_row("INV-1", "Walk-in", "1000.00", "92.00", "92.00", rate="18")]
    assert any("is not 18% of 1000.00" in p for p in validate(rows, HOME))


def test_tax_within_a_rupee_of_the_rate_is_rounding_not_a_problem():
    rows = [_row("INV-1", "Walk-in", "1000.00", "90.40", "90.40", rate="18")]
    assert validate(rows, HOME) == []


def test_an_intra_state_supply_charging_igst_is_a_problem():
    rows = [_row("INV-1", "Acme", "1000.00", igst="180.00", gstin=MH_BUYER)]
    assert any("intra-state supply must charge CGST+SGST" in p for p in validate(rows, HOME))


def test_an_inter_state_supply_charging_cgst_and_sgst_is_a_problem():
    rows = [_row("INV-1", "Bharat", "1000.00", "90.00", "90.00", gstin=KA_BUYER)]
    assert any("inter-state supply must charge IGST" in p for p in validate(rows, HOME))


# --- the tool, against the fake Tally ----------------------------------------


def _sale(number: str, party: str, taxes: dict[str, str]) -> Voucher:
    taxable = Decimal("10000.00")
    tax = sum((Decimal(v) for v in taxes.values()), Decimal("0"))
    return Voucher(
        voucher_type=VoucherType.SALES,
        date=date(2026, 6, 15),
        party_name=party,
        reference=number,
        narration="Sale of goods",
        lines=[
            VoucherLine(ledger_name=party, amount=taxable + tax),
            VoucherLine(ledger_name="Sales - GST 18%", amount=-taxable),
            *(
                VoucherLine(ledger_name=ledger, amount=-Decimal(amount))
                for ledger, amount in taxes.items()
            ),
        ],
    )


@pytest.fixture
def ctx(backend, company) -> ToolContext:
    return ToolContext(backend=backend, company=company)


async def test_the_export_writes_the_json_and_a_summary_from_the_books(
    ctx, backend, fake_tally, tmp_path
):  # type: ignore[no-untyped-def]
    fake_tally.add_ledger("Walk-in Customer", "Sundry Debtors", state="Maharashtra")
    for voucher in (
        _sale("INV-001", "Acme Industries", {"Output CGST": "900", "Output SGST": "900"}),
        _sale("INV-002", "Bharat Supplies", {"Output IGST": "1800"}),
        _sale("INV-003", "Walk-in Customer", {"Output CGST": "900", "Output SGST": "900"}),
    ):
        await backend.create_voucher(voucher, make_key("Demo", voucher))

    result = await gstr1_export(ctx, "2026-06", out_dir=str(tmp_path))

    assert result.data["problems"] == []
    written = json.loads(
        (tmp_path / ctx.company.name / "2026-06.json").read_text(encoding="utf-8")
    )
    assert written["fp"] == "062026"
    assert [p["ctin"] for p in written["b2b"]] == ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4"]
    assert written["b2cs"][0]["sply_ty"] == "INTRA"
    summary = (tmp_path / ctx.company.name / "2026-06.md").read_text(encoding="utf-8")
    assert "B2B: 2 invoice(s)" in summary
    assert "| 18% | 30000.00 | 5400.00 |" in summary


async def test_a_month_with_problems_writes_no_json_and_says_why(
    ctx, backend, tmp_path
):  # type: ignore[no-untyped-def]
    bad = _sale("INV-001", "Bharat Supplies", {"Output CGST": "900", "Output SGST": "900"})
    await backend.create_voucher(bad, make_key("Demo", bad))

    result = await gstr1_export(ctx, "2026-06", out_dir=str(tmp_path))

    assert any("inter-state" in p for p in result.data["problems"])
    assert not (tmp_path / ctx.company.name / "2026-06.json").exists()
    assert "No JSON was written" in (
        tmp_path / ctx.company.name / "2026-06.md"
    ).read_text(encoding="utf-8")


async def test_a_company_name_cannot_steer_the_export_out_of_its_folder(
    backend, company, tmp_path
):  # type: ignore[no-untyped-def]
    hostile = company.model_copy(update={"name": "..\\..\\Shah: Co"})
    ctx = ToolContext(backend=backend, company=hostile)

    result = await gstr1_export(ctx, "2026-06", out_dir=str(tmp_path / "out"))

    summary = Path(result.data["summary_path"]).resolve()
    assert summary.parent.parent == (tmp_path / "out").resolve()
    assert summary.parent.name == "..-..-Shah- Co"


def test_the_export_is_registered_as_a_read():
    reads = {s["name"] for s in registry.schemas(mutating=False)}
    assert "gstr1_export" in reads
