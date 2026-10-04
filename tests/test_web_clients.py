"""One browser tab for every client the practice works on.

What is being protected: a partner switching clients in the web UI must only
ever act on the books in the address bar. Each client keeps its own queue and
audit chain in its own database, so a ticket from one client is not reachable
under another client's address, while the people who decide - registered once
for the firm - can decide on any of them.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tallyagent_agent.memory import Memory
from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.people import PARTNER, People
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_channels.pool import Entry, ServicesPool
from tallyagent_channels.services import Services
from tallyagent_channels.web.app import build_router
from tallyagent_core.models import Company, Period
from tallyagent_core.policy import Policy
from tallyagent_llm import mock
from tallyagent_llm.router import Router
from tallyagent_tally.backend import TallyBackend
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.fake_server import seeded_demo
from tallyagent_tools import vouchers
from tallyagent_tools.base import ToolContext
from tallyagent_tools.executor import build_executor

FY = Period(start=date(2026, 4, 1), end=date(2027, 3, 31), locked_before=date(2026, 4, 1))


def _wire(company_name: str, people: People):
    """One client's services: its own fake Tally, its own database."""
    tally = seeded_demo(company_name)
    engine = make_engine(":memory:")
    audit = AuditLog(engine)
    queue = ApprovalQueue(engine, audit)
    company = Company(name=company_name, state_code="27", period=FY)
    backend = TallyBackend(
        TallyClient(
            TallyConfig(host="127.0.0.1", port=9000, company=company_name),
            transport=tally.transport,
        )
    )
    tools = ToolContext(
        backend=backend,
        company=company,
        policy=Policy.default(),
        enqueue=queue.enqueue,
        source="web",
    )
    queue.executor = build_executor(tools)
    services = Services(
        company=company,
        tools=tools,
        queue=queue,
        audit=audit,
        router=Router(mock.MockProvider()),
        memory=Memory(engine, company.name),
        people=people,
    )
    return services, tally


@pytest.fixture
def firm():
    """The people register, once for the practice."""
    engine = make_engine(":memory:")
    people = People(engine, AuditLog(engine))
    people.add("R. Mehta", PARTNER, "4821")
    return people


@pytest.fixture
def practice(firm):
    sharma, sharma_tally = _wire("Sharma Textiles", firm)
    gupta, gupta_tally = _wire("Gupta Foods", firm)
    wired = {"sharma": sharma, "gupta": gupta}
    built: list[str] = []

    def factory(slug: str) -> Services:
        built.append(slug)
        return wired[slug]

    pool = ServicesPool(
        entries=[Entry("sharma", "Sharma Textiles"), Entry("gupta", "Gupta Foods")],
        factory=factory,
    )
    return {
        "pool": pool,
        "sharma": sharma,
        "gupta": gupta,
        "tallies": {"sharma": sharma_tally, "gupta": gupta_tally},
        "built": built,
    }


@pytest.fixture
def client(practice):
    app = FastAPI()
    app.include_router(build_router(practice["pool"]))
    return TestClient(app)


async def queue_a_sale(services, reference="INV-001"):
    await vouchers.create_sales_voucher(
        services.tools,
        "Acme Industries",
        "10000.00",
        "18",
        date(2026, 6, 15),
        reference=reference,
    )


# --- the picker ----------------------------------------------------------------


def test_the_picker_lists_every_client_by_slug_and_name(client):
    body = client.get("/c/sharma/").text

    assert "sharma &mdash; Sharma Textiles" in body or "sharma — Sharma Textiles" in body
    assert "gupta &mdash; Gupta Foods" in body or "gupta — Gupta Foods" in body
    assert 'value="/c/sharma/" selected' in body


async def test_the_picker_counts_pending_tickets_per_client(client, practice):
    await queue_a_sale(practice["sharma"], "S-1")
    await queue_a_sale(practice["sharma"], "S-2")
    await queue_a_sale(practice["gupta"], "G-1")

    body = client.get("/c/gupta/").text

    assert 'data-slug="sharma" data-pending="2"' in body
    assert 'data-slug="gupta" data-pending="1"' in body


async def test_each_client_page_shows_only_its_own_queue(client, practice):
    await queue_a_sale(practice["sharma"], "SHARMA-INV")

    assert "SHARMA-INV" in client.get("/c/sharma/approvals").text
    assert "Nothing waiting" in client.get("/c/gupta/approvals").text


def test_a_client_is_wired_only_when_first_asked_for(practice):
    pool = practice["pool"]

    pool.get("gupta")
    pool.get("gupta")

    assert practice["built"] == ["gupta"]


def test_an_unknown_client_is_not_found(client):
    assert client.get("/c/nobody/").status_code == 404


# --- decisions stay in their own books -----------------------------------------


async def test_a_ticket_cannot_be_approved_under_another_clients_address(
    client, practice
):
    await queue_a_sale(practice["sharma"])

    response = client.post(
        "/c/gupta/approvals/APR-0001/approve", data={"who": "R. Mehta", "pin": "4821"}
    )

    assert response.status_code == 404
    assert practice["sharma"].queue.get("APR-0001").status == "pending"
    assert practice["tallies"]["sharma"].vouchers == []
    assert practice["tallies"]["gupta"].vouchers == []


async def test_a_ticket_cannot_be_rejected_or_edited_under_another_clients_address(
    client, practice
):
    await queue_a_sale(practice["sharma"])

    reject = client.post(
        "/c/gupta/approvals/APR-0001/reject",
        data={"who": "R. Mehta", "pin": "4821", "reason": "wrong"},
    )
    edit = client.post(
        "/c/gupta/approvals/APR-0001/edit", data={"who": "R. Mehta", "pin": "4821"}
    )

    assert reject.status_code == 404
    assert edit.status_code == 404
    assert practice["sharma"].queue.get("APR-0001").status == "pending"


async def test_the_firms_partner_can_approve_on_every_client(client, practice):
    await queue_a_sale(practice["sharma"])
    await queue_a_sale(practice["gupta"])

    for slug in ("sharma", "gupta"):
        response = client.post(
            f"/c/{slug}/approvals/APR-0001/approve",
            data={"who": "R. Mehta", "pin": "4821"},
        )
        assert "posted to Tally" in response.text
        assert practice[slug].queue.get("APR-0001").decided_by == "R. Mehta"
        assert len(practice["tallies"][slug].vouchers) == 1


def test_chat_on_a_client_answers_from_that_clients_books(client, practice):
    client.post("/c/gupta/chat", data={"message": "what is outstanding?"})

    assert "web" in practice["gupta"]._agents
    assert practice["sharma"]._agents == {}


# --- the old addresses ---------------------------------------------------------


def test_the_bare_address_redirects_to_the_default_client(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/c/sharma/"


def test_an_old_approvals_address_keeps_its_query(client):
    response = client.get("/approvals?status=approved", follow_redirects=False)

    assert response.headers["location"] == "/c/sharma/approvals?status=approved"


async def test_an_old_approve_post_lands_on_the_default_client(client, practice):
    await queue_a_sale(practice["sharma"])

    response = client.post(
        "/approvals/APR-0001/approve", data={"who": "R. Mehta", "pin": "4821"}
    )

    assert "posted to Tally" in response.text
    assert len(practice["tallies"]["sharma"].vouchers) == 1


def test_a_single_company_install_is_a_pool_of_one(practice):
    pool = ServicesPool.single(practice["sharma"])

    assert pool.slugs() == ["default"]
    assert pool.get("default") is practice["sharma"]


# --- review fixes ----------------------------------------------------------------


def test_one_client_that_cannot_be_wired_does_not_take_the_others_down(firm):
    sharma, _ = _wire("Sharma Textiles", firm)

    def factory(slug: str) -> Services:
        if slug == "broken":
            raise OSError("database file is locked")
        return sharma

    pool = ServicesPool(
        entries=[Entry("sharma", "Sharma Textiles"), Entry("broken", "Broken Ltd")],
        factory=factory,
    )
    app = FastAPI()
    app.include_router(build_router(pool))

    response = TestClient(app).get("/c/sharma/")

    assert response.status_code == 200
    assert "broken &mdash; Broken Ltd (unavailable)" in response.text


def test_serve_without_a_client_is_wired_to_the_first_registered_client():
    from tallyagent_daemon.cli import _serve_client
    from tallyagent_daemon.clients import Client, Register

    register = Register(
        clients=[
            Client(slug="sharma", name="S", company="Sharma"),
            Client(slug="gupta", name="G", company="Gupta"),
        ]
    )

    assert _serve_client(register, "") == "sharma"
    assert _serve_client(register, "gupta") == "gupta"
    assert _serve_client(Register(), "") == ""
