"""Stage 7: MCP tool listing, read execution, writes returning tickets."""

from __future__ import annotations

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.errors import NotConfiguredError
from tallyagent_core.policy import Policy
from tallyagent_mcp_server import server as mcp_server
from tallyagent_tools.base import PendingAction, ToolContext


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def queue(engine, backend, company):
    async def execute(action: PendingAction):
        return await backend.create_voucher(
            action.voucher, action.idempotency_key, company.name
        )

    return ApprovalQueue(engine, AuditLog(engine), execute)


@pytest.fixture
def ctx(backend, company, queue):
    return ToolContext(
        backend=backend,
        company=company,
        policy=Policy.default(),
        enqueue=queue.enqueue,
        source="mcp",
    )


# --- listing ----------------------------------------------------------------


def test_every_registry_tool_is_listed_with_a_schema():
    tools = mcp_server.list_tools()
    names = {t["name"] for t in tools}
    assert "trial_balance" in names
    assert "create_sales_voucher" in names
    assert len(tools) == len(mcp_server.registry.TOOLS)
    for tool in tools:
        assert tool["description"]
        assert tool["input_schema"]["type"] == "object"


def test_write_tools_advertise_that_they_only_queue():
    tools = {t["name"]: t for t in mcp_server.list_tools()}
    assert "returns an approval-queue ticket" in tools["create_sales_voucher"]["description"]
    assert "approval-queue ticket" not in tools["trial_balance"]["description"]


def test_read_only_mode_hides_write_tools():
    names = {t["name"] for t in mcp_server.list_tools(read_only=True)}
    assert "trial_balance" in names
    assert not {n for n in names if n.startswith("create_")}


# --- calls ------------------------------------------------------------------


async def test_a_read_tool_executes_directly(ctx):
    outcome = await mcp_server.call_tool(ctx, "cash_position", {})
    assert not outcome.is_error
    assert "Cash and bank: 265000.00" in outcome.text
    assert outcome.data["accounts"]["Cash"] == "15000.00"
    assert outcome.to_content()[0]["type"] == "text"


async def test_a_write_tool_returns_a_ticket_and_mutates_nothing(ctx, queue, fake_tally):
    outcome = await mcp_server.call_tool(
        ctx,
        "create_sales_voucher",
        {
            "party_name": "Acme Industries",
            "taxable_value": "10000.00",
            "gst_rate": "18",
            "voucher_date": "2026-06-15",
            "reference": "MCP-1",
        },
    )
    assert not outcome.is_error
    assert outcome.ticket == "APR-0001"
    assert fake_tally.vouchers == [], "an MCP client must never be able to post"

    item = queue.get("APR-0001")
    assert item.status == "pending"
    assert item.source == "mcp"


async def test_a_write_that_fails_validation_is_an_error_not_a_ticket(ctx, fake_tally):
    outcome = await mcp_server.call_tool(
        ctx,
        "create_sales_voucher",
        {
            "party_name": "Nobody Ltd",
            "taxable_value": "1000.00",
            "voucher_date": "2026-06-15",
        },
    )
    assert outcome.is_error
    assert not outcome.ticket
    assert "Not queued" in outcome.text
    assert fake_tally.vouchers == []


async def test_a_write_with_no_queue_configured_is_refused(backend, company, fake_tally):
    ctx = ToolContext(backend=backend, company=company, policy=Policy.default())
    outcome = await mcp_server.call_tool(
        ctx, "create_receipt", {"party_name": "Acme Industries", "amount": "100"}
    )
    assert outcome.is_error
    assert "no approval queue" in outcome.text
    assert fake_tally.vouchers == []


async def test_policy_auto_approval_is_labelled_as_such(backend, company, fake_tally, queue):
    policy = Policy.from_dict(
        {"actions": {"create_receipt": {"mode": "auto_below_amount", "max_amount": 5000}}}
    )
    ctx = ToolContext(
        backend=backend, company=company, policy=policy, enqueue=queue.enqueue
    )
    outcome = await mcp_server.call_tool(
        ctx,
        "create_receipt",
        {"party_name": "Acme Industries", "amount": "100", "voucher_date": "2026-06-20"},
    )
    assert "auto-approved by policy, not by a person" in outcome.text
    assert len(fake_tally.vouchers) == 1


async def test_unknown_tools_and_bad_arguments_are_errors_not_crashes(ctx):
    unknown = await mcp_server.call_tool(ctx, "drop_everything", {})
    assert unknown.is_error and "Available:" in unknown.text

    bad = await mcp_server.call_tool(ctx, "cash_position", {"not_a_parameter": 1})
    assert bad.is_error and "wrong arguments" in bad.text


# --- transport --------------------------------------------------------------


def test_http_transport_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("TALLYAGENT_MCP_TOKEN", raising=False)
    with pytest.raises(NotConfiguredError, match="TALLYAGENT_MCP_TOKEN"):
        mcp_server.bearer_token()


def test_bearer_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TALLYAGENT_MCP_TOKEN", "s3cret")
    assert mcp_server.bearer_token() == "s3cret"
    assert mcp_server.bearer_token("explicit") == "explicit"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer s3cret", True),
        ("bearer s3cret", True),
        ("Bearer wrong", False),
        ("s3cret", False),
        ("", False),
        (None, False),
    ],
)
def test_bearer_header_checking(header, expected):
    assert mcp_server.check_bearer(header, "s3cret") is expected


def test_missing_sdk_says_how_to_install_it(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("mcp"):
            raise ImportError("no mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(NotConfiguredError, match="uv sync --extra mcp"):
        mcp_server._require_sdk()


def test_server_builds_when_the_sdk_is_present(ctx):
    pytest.importorskip("mcp")
    server = mcp_server.build_server(ctx)
    assert server.name == "tallyagent"


async def test_sdk_handlers_list_tools_and_queue_a_write(ctx, fake_tally, queue):
    """Exercise the handlers the SDK actually calls, not just our helpers."""
    pytest.importorskip("mcp")
    import mcp.types as types

    server = mcp_server.build_server(ctx)
    list_handler = server.get_request_handler("tools/list").handler
    call_handler = server.get_request_handler("tools/call").handler

    listed = await list_handler(None, None)
    names = {tool.name for tool in listed.tools}
    assert "trial_balance" in names and "create_sales_voucher" in names

    read = await call_handler(
        None, types.CallToolRequestParams(name="cash_position", arguments={})
    )
    assert not read.is_error
    assert "Cash and bank" in read.content[0].text

    write = await call_handler(
        None,
        types.CallToolRequestParams(
            name="create_sales_voucher",
            arguments={
                "party_name": "Acme Industries",
                "taxable_value": "10000.00",
                "gst_rate": "18",
                "voucher_date": "2026-06-15",
                "reference": "MCP-SDK-1",
            },
        ),
    )
    assert not write.is_error
    assert "APR-0001" in write.content[0].text
    assert fake_tally.vouchers == []
    assert queue.get("APR-0001").status == "pending"


async def test_http_transport_rejects_a_missing_or_wrong_token(ctx, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("TALLYAGENT_MCP_TOKEN", "s3cret")
    app = mcp_server.build_http_app(ctx)

    async def probe(authorization: str | None):
        headers = [(b"host", b"testserver")]
        if authorization is not None:
            headers.append((b"authorization", authorization.encode()))
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": headers,
                "query_string": b"",
            },
            receive,
            send,
        )
        return sent[0]["status"]

    assert await probe(None) == 401
    assert await probe("Bearer wrong") == 401
