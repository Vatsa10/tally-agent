"""The deliverables, exercised end to end against the fake Tally.

Each test here corresponds to a line of the checklist in SPEC.md.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tally.fake_server import seeded_demo
from tallyagent_tools import reconcile
from tallyagent_tools.base import ToolContext

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
COMPANY = "Demo Traders Pvt Ltd"


@pytest.fixture
def demo(tmp_path):
    """The demo as a user runs it: real config, mock model, fake Tally."""
    config = config_mod.load(
        ROOT / "config" / "config.example.toml", ROOT / "config" / "policy.example.toml"
    )
    config.company = config.company.model_copy(update={"name": COMPANY})
    config.tally.company = COMPANY
    config.tally.host = "fake-tally"
    config.model.provider = "mock"
    config.db_path = str(tmp_path / "demo.db")
    config.logging.egress_log = str(tmp_path / "egress.jsonl")

    tally = seeded_demo(COMPANY)
    wired = wiring.build(config, transport=tally.transport)
    wired.services.router = Router(mock.MockProvider())
    return wired, tally


# --- probe ------------------------------------------------------------------


async def test_probe_detects_tally_and_lists_the_demo_company(demo):
    wired, _ = demo
    info = await wired.backend.probe()
    assert info["status"] == "ok"
    assert info["companies"] == COMPANY


# --- chat -------------------------------------------------------------------


async def test_chat_answers_only_from_tool_results(demo):
    wired, _ = demo
    result = await wired.services.agent("demo").run("what is the cash position?")
    assert result.tools_used == ["cash_position"]
    assert "265000.00" in result.text

    # And refuses to invent one when it has no tool result.
    vague = await wired.services.agent("demo2").run(
        "roughly what did we sell last year?"
    )
    assert vague.tools_used == []
    assert "will not state a figure" in vague.text


# --- invoice image to posted voucher ----------------------------------------


async def test_invoice_queues_then_posts_and_a_resubmission_is_a_no_op(demo):
    """The central deliverable, start to finish."""
    wired, tally = demo
    services = wired.services
    tools: ToolContext = services.tools
    extraction = (FIXTURES / "invoice_extraction.json").read_text()

    # 1. The image's extraction becomes a validated, queued draft.
    from tallyagent_tools import ingest

    first = await ingest.invoice_image_to_draft(tools, extraction)
    assert first.ok, first.message
    ticket = first.data["ticket"]
    assert tally.vouchers == [], "nothing may post before approval"

    # 2. The diff a person reads is correct: inter-state purchase, IGST, balanced.
    item = services.queue.get(ticket)
    diff = item.diff()
    assert diff["amount"] == "5900.00"
    assert diff["totals"] == {"debit": "5900.00", "credit": "5900.00"}
    impact = {row["ledger"]: (row["debit"], row["credit"]) for row in diff["ledger_impact"]}
    assert impact["Bharat Supplies"] == ("", "5900.00")
    assert impact["Purchase - GST 18%"] == ("5000.00", "")
    assert impact["Input IGST"] == ("900.00", "")
    assert "Output CGST" not in impact, "Karnataka supplier must attract IGST"
    assert diff["validation"]["ok"]

    # 3. Approving posts it.
    write = await services.queue.approve(ticket, "ca@firm.in")
    assert write.ok, write.errors
    assert len(tally.vouchers) == 1
    assert tally.balance("Bharat Supplies") == Decimal("-5900.00")
    assert tally.balance("Input IGST") == Decimal("900.00")

    # 4. The same image again: duplicate detection stops it before the queue.
    second = await ingest.invoice_image_to_draft(tools, extraction)
    assert not second.ok
    assert any(r.rule == "not_duplicate" for r in second.validation.failures)
    assert len(tally.vouchers) == 1

    # 5. And even bypassing that, the idempotency key replays rather than posts.
    from tallyagent_core.idempotency import make_key

    replay = await wired.backend.create_voucher(
        item.voucher, make_key(COMPANY, item.voucher), COMPANY
    )
    assert replay.replayed and replay.ok
    assert len(tally.vouchers) == 1

    # 6. The whole sequence is in an intact audit chain.
    assert services.audit.verify().ok
    events = [e.event for e in services.audit.entries()]
    assert events == ["action_queued", "action_approved", "action_executed"]


# --- reconciliation ---------------------------------------------------------


async def test_bank_reco_on_the_fixture(demo):
    wired, _ = demo
    tools = wired.services.tools
    from tallyagent_core.idempotency import make_key
    from tallyagent_core.models import Voucher, VoucherLine, VoucherType

    receipt = Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=date(2026, 6, 15),
        party_name="Acme Industries",
        narration="Acme Industries NEFT",
        lines=[
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("11800.00")),
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("-11800.00")),
        ],
    )
    await wired.backend.create_voucher(receipt, make_key(COMPANY, receipt), COMPANY)

    from tallyagent_tools import ingest

    rows = ingest.parse_bank_statement_csv(
        (FIXTURES / "bank_statement.csv").read_text()
    )
    result = await reconcile.bank_reco(tools, rows, "Bank - HDFC 1234")

    assert [m["amount"] for m in result.data["matched"]] == ["11800.00"]
    unmatched = {r["amount"] for r in result.data["unmatched_statement"]}
    assert unmatched == {"-5900.00", "-118.00", "50000.00"}
    proposals = {p["amount"]: p["tool"] for p in result.data["proposals"]}
    assert proposals["50000.00"] == "create_receipt"
    assert proposals["118.00"] == "create_payment"
    assert "none posted" in result.message


async def test_gstr2b_reco_on_the_fixture(demo):
    wired, _ = demo
    tools = wired.services.tools
    from tallyagent_core.idempotency import make_key
    from tallyagent_core.models import Voucher, VoucherLine, VoucherType

    for reference, taxable, igst in (
        ("BS/2026/41", "5000.00", "900.00"),
        ("BS/2026/42", "9000.00", "1620.00"),
        ("BS/2026/99", "1000.00", "180.00"),
    ):
        voucher = Voucher(
            voucher_type=VoucherType.PURCHASE,
            date=date(2026, 6, 10),
            party_name="Bharat Supplies",
            reference=reference,
            lines=[
                VoucherLine(
                    ledger_name="Bharat Supplies",
                    amount=-(Decimal(taxable) + Decimal(igst)),
                ),
                VoucherLine(ledger_name="Purchase - GST 18%", amount=Decimal(taxable)),
                VoucherLine(ledger_name="Input IGST", amount=Decimal(igst)),
            ],
        )
        await wired.backend.create_voucher(voucher, make_key(COMPANY, voucher), COMPANY)

    gstr2b = json.loads((FIXTURES / "gstr2b.json").read_text())
    result = await reconcile.gstr2b_vs_purchase_register(
        tools, gstr2b, date(2026, 6, 1), date(2026, 6, 30)
    )
    assert result.data["counts"] == {
        "matched": 1,
        "value_mismatch": 1,
        "missing_in_books": 1,
        "missing_in_2b": 1,
    }
    assert result.data["csv"].startswith("status,supplier,")


# --- MCP --------------------------------------------------------------------


async def test_mcp_lists_tools_and_a_write_returns_a_ticket(demo):
    wired, tally = demo
    from tallyagent_mcp_server import server as mcp_server

    tools = mcp_server.list_tools()
    assert len(tools) >= 20
    assert {"trial_balance", "create_sales_voucher"} <= {t["name"] for t in tools}

    outcome = await mcp_server.call_tool(
        wired.services.tools,
        "create_sales_voucher",
        {
            "party_name": "Acme Industries",
            "taxable_value": "10000.00",
            "gst_rate": "18",
            "voucher_date": "2026-06-15",
            "reference": "E2E-MCP",
        },
    )
    assert outcome.ticket == "APR-0001"
    assert tally.vouchers == []


# --- WhatsApp ---------------------------------------------------------------


def test_whatsapp_handles_a_signed_payload_end_to_end(demo, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tallyagent_channels.whatsapp import meta
    from tallyagent_channels.whatsapp.webhook import build_router

    wired, _ = demo
    sent: list[dict] = []

    def graph(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    config = meta.WhatsAppConfig(
        enabled=True,
        verify_token="verify-me",
        app_secret="app-secret",
        access_token="token",
        phone_number_id="PN1",
        allowlist={"+919876543210": COMPANY},
    )
    client = meta.MetaClient(config, transport=httpx.MockTransport(graph))

    app = FastAPI()
    app.include_router(build_router(wired.services, config, client))
    http = TestClient(app)

    challenge = http.get(
        "/whatsapp/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "42",
        },
    )
    assert challenge.text == "42"

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.e2e",
                                    "type": "text",
                                    "text": {"body": "what is the cash position?"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    response = http.post(
        "/whatsapp/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": meta.sign(body, "app-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert "265000.00" in sent[0]["text"]["body"]

    # An unsigned copy of the same payload is refused.
    refused = http.post(
        "/whatsapp/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=wrong"},
    )
    assert refused.status_code == 401
    assert len(sent) == 1


# --- docs -------------------------------------------------------------------


def test_architecture_documents_the_tiers_and_the_trust_boundary():
    text = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    for marker in ("TIER 1", "TIER 2", "TIER 3", "TRUST BOUNDARY"):
        assert marker in text
    assert "Crosses the boundary" in text
    assert "Never crosses" in text


def test_security_covers_the_named_threats():
    text = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    for threat in (
        "Prompt injection through invoice images",
        "Malicious WhatsApp media",
        "LAN exposure of Tally's port 9000",
    ):
        assert threat in text


def test_decisions_records_assumptions_and_gaps():
    text = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    assert "## Incomplete" in text
    assert text.count("- **D-") >= 10
