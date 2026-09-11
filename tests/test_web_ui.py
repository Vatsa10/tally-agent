"""Stage 8: the local approvals and chat UI."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tallyagent_agent.memory import Memory
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_channels.services import Services
from tallyagent_channels.web.app import build_router
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tools.base import PendingAction, ToolContext


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def services(engine, backend, company):
    audit = AuditLog(engine)

    async def execute(action: PendingAction):
        return await backend.create_voucher(
            action.voucher, action.idempotency_key, company.name
        )

    queue = ApprovalQueue(engine, audit, execute)
    tools = ToolContext(
        backend=backend, company=company, policy=Policy.default(), enqueue=queue.enqueue,
        source="web",
    )
    return Services(
        company=company,
        tools=tools,
        queue=queue,
        audit=audit,
        router=Router(mock.MockProvider()),
        memory=Memory(engine, company.name),
    )


@pytest.fixture
def client(services):
    app = FastAPI()
    app.include_router(build_router(services))
    return TestClient(app)


async def queue_a_sale(services, reference="INV-001"):
    from datetime import date

    from tallyagent_tools import vouchers

    await vouchers.create_sales_voucher(
        services.tools,
        "Acme Industries",
        "10000.00",
        "18",
        date(2026, 6, 15),
        reference=reference,
    )


# --- pages ------------------------------------------------------------------


def test_index_renders_with_nothing_queued(client, services):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert services.company.name in body
    assert "Nothing waiting" in body
    assert "Audit chain intact" in body
    assert "nothing is posted to tally until you approve it here" in body.lower()


async def test_index_lists_a_queued_voucher(client, services):
    await queue_a_sale(services)
    body = client.get("/").text
    assert "APR-0001" in body
    assert "Sales invoice INV-001 to Acme Industries" in body
    assert "Approve &amp; post" in body


async def test_diff_partial_shows_impact_validation_and_xml(client, services):
    await queue_a_sale(services)
    body = client.get("/approvals/APR-0001").text
    assert "Output CGST" in body
    assert "11800.00" in body
    assert "PASS &mdash; balanced" in body or "PASS — balanced" in body
    assert "VOUCHERTYPENAME" in body
    assert "XML that will be sent to Tally" in body


def test_health_reports_status(client, services):
    data = client.get("/health").json()
    assert data["company"] == services.company.name
    assert data["pending"] == 0
    assert data["provider"] == "mock"


# --- decisions --------------------------------------------------------------


async def test_approving_posts_and_clears_the_queue(client, services, fake_tally):
    await queue_a_sale(services)
    response = client.post("/approvals/APR-0001/approve", data={"actor": "ca@firm.in"})
    assert response.status_code == 200
    assert "posted to Tally" in response.text
    assert "Nothing waiting" in response.text
    assert len(fake_tally.vouchers) == 1
    assert services.queue.get("APR-0001").decided_by == "ca@firm.in"


async def test_rejecting_needs_a_reason(client, services, fake_tally):
    await queue_a_sale(services)
    missing = client.post("/approvals/APR-0001/reject", data={})
    assert missing.status_code == 422  # the form field is required

    response = client.post(
        "/approvals/APR-0001/reject", data={"reason": "wrong customer"}
    )
    assert "APR-0001 rejected." in response.text
    assert fake_tally.vouchers == []
    assert services.queue.get("APR-0001").decision_reason == "wrong customer"


async def test_edit_and_approve_applies_the_substitution_and_learns(
    client, services, fake_tally
):
    from datetime import date

    from tallyagent_tools import vouchers

    await vouchers.create_journal(
        services.tools,
        lines=[
            {"ledger": "Cash", "amount": "1000.00"},
            {"ledger": "Sales - GST 18%", "amount": "-1000.00"},
        ],
        voucher_date=date(2026, 6, 15),
        narration="misc income",
    )
    response = client.post(
        "/approvals/APR-0001/edit",
        data={
            "ledger_0": "Bank - HDFC 1234",
            "ledger_1": "Sales - GST 18%",
            "reason": "it went to the bank",
        },
    )
    assert "edited and posted" in response.text
    assert fake_tally.balance("Bank - HDFC 1234") == fake_tally.ledgers[
        "Bank - HDFC 1234"
    ].opening_balance + 1000
    assert services.tools.ledger_aliases == {"Cash": "Bank - HDFC 1234"}


async def test_an_edit_that_unbalances_is_refused_before_it_reaches_tally(
    client, services, fake_tally
):
    await queue_a_sale(services)
    response = client.post(
        "/approvals/APR-0001/edit",
        data={"amount_0": "99999.00", "reason": "fat finger"},
    )
    assert "does not balance" in response.text
    assert fake_tally.vouchers == []
    assert services.queue.get("APR-0001").status == "pending"


async def test_a_partial_edit_form_keeps_the_original_lines(client, services, fake_tally):
    await queue_a_sale(services)
    response = client.post(
        "/approvals/APR-0001/edit", data={"reason": "no actual change"}
    )
    assert "edited and posted" in response.text
    assert len(fake_tally.vouchers) == 1
    assert fake_tally.balance("Acme Industries") == 11800


# --- chat -------------------------------------------------------------------


def test_chat_answers_from_a_tool_and_shows_the_trace(client):
    response = client.post("/chat", data={"message": "what is outstanding?"})
    body = response.text
    assert "Total receivable" in body
    assert "outstanding_receivables" in body
    assert "tok" in body and "B sent" in body


def test_chat_that_queues_a_write_says_nothing_is_posted(client, services, fake_tally):
    services.router = Router(
        mock.MockProvider(
            script=[
                mock.call(
                    "create_sales_voucher",
                    party_name="Acme Industries",
                    taxable_value="10000.00",
                    gst_rate="18",
                    voucher_date="2026-06-15",
                    reference="WEB-1",
                ),
                mock.text("Drafted the invoice."),
            ]
        )
    )
    services.reset("web")
    body = client.post("/chat", data={"message": "invoice Acme 10000"}).text
    assert "APR-0001" in body
    assert "nothing has been posted yet" in body
    assert fake_tally.vouchers == []


def test_separate_conversations_do_not_share_history(client, services):
    client.post("/chat", data={"message": "cash position?", "conversation": "a"})
    client.post("/chat", data={"message": "cash position?", "conversation": "b"})
    assert set(services._agents) == {"a", "b"}
    assert services.agent("a") is not services.agent("b")
