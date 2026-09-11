"""Stage 5: provider swap by config, wire formats, cost accounting, egress log."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from tallyagent_core.errors import NotConfiguredError, TallyAgentError
from tallyagent_llm import mock
from tallyagent_llm.anthropic_ import AnthropicProvider, to_anthropic
from tallyagent_llm.deepseek import DeepSeekProvider, to_openai_messages
from tallyagent_llm.openai_ import OpenAIProvider
from tallyagent_llm.provider import Image, Message, ToolCall
from tallyagent_llm.router import ModelConfig, Router, build_provider, estimate_cost

PNG_BYTES = b"fake-png-bytes"

CONVERSATION = [
    Message(role="system", content="You are a bookkeeper."),
    Message(role="user", content="What is outstanding?"),
    Message(
        role="assistant",
        tool_calls=[ToolCall(id="c1", name="outstanding_receivables", arguments={})],
    ),
    Message(role="tool", tool_call_id="c1", content='{"total": "11800.00"}'),
]


def openai_transport(captured, usage=None, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        message: dict = {"content": "Acme owes 11,800.00."}
        if calls:
            message = {"content": None, "tool_calls": calls}
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": message}],
                "usage": usage or {"prompt_tokens": 1200, "completion_tokens": 40},
            },
        )

    return httpx.MockTransport(handler)


# --- wire formats -----------------------------------------------------------


def test_openai_message_translation():
    wire = to_openai_messages(CONVERSATION)
    assert wire[0] == {"role": "system", "content": "You are a bookkeeper."}
    assert wire[2]["tool_calls"][0]["function"]["name"] == "outstanding_receivables"
    assert wire[3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": '{"total": "11800.00"}',
    }


def test_openai_images_become_data_urls():
    message = Message(
        role="user",
        content="read this",
        images=[Image(data=PNG_BYTES, media_type="image/png")],
    )
    parts = to_openai_messages([message])[0]["content"]
    assert parts[0] == {"type": "text", "text": "read this"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_anthropic_hoists_system_and_wraps_tool_results():
    system, messages = to_anthropic(CONVERSATION)
    assert system == "You are a bookkeeper."
    assert messages[0]["role"] == "user"
    assert messages[1]["content"][0]["type"] == "tool_use"
    result_block = messages[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == "c1"


def test_tool_call_survives_malformed_arguments():
    call = ToolCall.from_json("c1", "create_receipt", "{not json")
    assert call.arguments == {"__parse_error__": "{not json"}
    assert ToolCall.from_json("c2", "x", "").arguments == {}


# --- providers --------------------------------------------------------------


async def test_deepseek_posts_openai_shape_and_reports_usage():
    captured: list[dict] = []
    provider = DeepSeekProvider(
        "sk-test",
        transport=openai_transport(
            captured,
            {
                "prompt_tokens": 1200,
                "completion_tokens": 40,
                "prompt_cache_hit_tokens": 1000,
            },
        ),
    )
    completion = await provider.complete(CONVERSATION, tools=[{"type": "function"}])

    body = captured[0]
    assert body["model"] == "deepseek-flash"
    assert body["tool_choice"] == "auto"
    assert body["temperature"] == 0.0
    assert completion.text == "Acme owes 11,800.00."
    assert completion.usage.prompt_tokens == 1200
    assert completion.usage.cached_tokens == 1000
    assert completion.raw_bytes_sent > 0


async def test_prompt_prefix_is_stable_across_turns():
    """Prefix caching only pays if the preamble never moves."""
    captured: list[dict] = []
    provider = DeepSeekProvider("sk-test", transport=openai_transport(captured))
    await provider.complete(CONVERSATION[:2])
    await provider.complete(CONVERSATION)
    first, second = captured
    assert second["messages"][: len(first["messages"])] == first["messages"]


async def test_deepseek_parses_tool_calls():
    provider = DeepSeekProvider(
        "sk-test",
        transport=openai_transport(
            [],
            calls=[
                {
                    "id": "c9",
                    "function": {
                        "name": "create_receipt",
                        "arguments": '{"party_name": "Acme Industries", "amount": "500"}',
                    },
                }
            ],
        ),
    )
    completion = await provider.complete(CONVERSATION)
    assert completion.wants_tools
    call = completion.tool_calls[0]
    assert call.name == "create_receipt"
    assert call.arguments["party_name"] == "Acme Industries"


async def test_http_errors_are_surfaced_not_swallowed():
    transport = httpx.MockTransport(lambda r: httpx.Response(429, text="rate limited"))
    provider = DeepSeekProvider("sk-test", transport=transport)
    with pytest.raises(TallyAgentError, match="HTTP 429"):
        await provider.complete(CONVERSATION)


async def test_empty_choices_is_an_error_not_an_empty_answer():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": []}))
    provider = DeepSeekProvider("sk-test", transport=transport)
    with pytest.raises(TallyAgentError, match="no choices"):
        await provider.complete(CONVERSATION)


async def test_anthropic_parses_text_and_tool_use():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["anthropic-version"]
        body = json.loads(request.content)
        assert body["system"] == "You are a bookkeeper."
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-5",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "cash_position",
                        "input": {},
                    },
                ],
                "usage": {"input_tokens": 900, "output_tokens": 30},
            },
        )

    provider = AnthropicProvider("sk-ant", transport=httpx.MockTransport(handler))
    completion = await provider.complete(CONVERSATION)
    assert completion.text == "Checking."
    assert completion.tool_calls[0].name == "cash_position"
    assert completion.usage.prompt_tokens == 900


async def test_openai_provider_reports_its_own_name():
    provider = OpenAIProvider("sk-oai", transport=openai_transport([]))
    completion = await provider.complete(CONVERSATION)
    assert completion.provider == "openai"


def test_missing_api_key_fails_with_actionable_advice():
    with pytest.raises(NotConfiguredError, match="DEEPSEEK_API_KEY"):
        DeepSeekProvider("")


# --- router -----------------------------------------------------------------


def test_provider_is_chosen_by_config_alone(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-3")

    assert build_provider(ModelConfig(provider="deepseek")).name == "deepseek"
    assert build_provider(ModelConfig(provider="anthropic")).name == "anthropic"
    assert build_provider(ModelConfig(provider="openai")).name == "openai"
    assert build_provider(ModelConfig(provider="mock")).name == "mock"


def test_unknown_provider_names_the_known_ones():
    with pytest.raises(NotConfiguredError, match="Known: mock"):
        build_provider(ModelConfig(provider="llamafile"))


def test_missing_key_points_at_the_mock(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("tallyagent_llm.router.api_key_for", lambda *a, **k: "")
    with pytest.raises(NotConfiguredError, match='provider = "mock"'):
        build_provider(ModelConfig(provider="deepseek"))


def test_cost_estimation():
    assert estimate_cost("deepseek-flash", 1_000_000, 0) == Decimal("0.280000")
    assert estimate_cost("mock-deterministic", 5000, 5000) == Decimal("0")
    assert estimate_cost("a-model-we-have-never-heard-of", 1000, 1000) == Decimal("0")


async def test_router_logs_egress_per_request():
    recorded = []
    router = Router(
        DeepSeekProvider("sk-test", transport=openai_transport([])),
        on_egress=recorded.append,
    )
    await router.complete(
        [Message(role="user", content="hi", images=[Image(data=PNG_BYTES)])]
    )
    record = recorded[0]
    assert record.destination == "https://api.deepseek.com"
    assert record.provider == "deepseek"
    assert record.bytes_sent > 0
    assert record.fields == ["user+1image"]
    assert record.prompt_tokens == 1200
    assert record.cost_usd > 0
    assert router.total_tokens == 1240
    assert router.total_cost == record.cost_usd


async def test_egress_log_records_shapes_not_content():
    router = Router(DeepSeekProvider("sk-test", transport=openai_transport([])))
    await router.complete(
        [Message(role="user", content="Acme owes us 1180000 - very confidential")]
    )
    record = router.egress[0]
    serialised = f"{record.destination}|{record.fields}|{record.bytes_sent}"
    assert "confidential" not in serialised
    assert "1180000" not in serialised


# --- mock provider ----------------------------------------------------------


async def test_mock_is_deterministic_and_calls_real_tools():
    provider = mock.MockProvider()
    first = await provider.complete(
        [Message(role="user", content="what is outstanding?")]
    )
    second = await provider.complete(
        [Message(role="user", content="what is outstanding?")]
    )
    assert first.tool_calls[0].name == "outstanding_receivables"
    assert second.tool_calls[0].name == "outstanding_receivables"


async def test_mock_script_is_replayed_in_order():
    provider = mock.MockProvider(
        script=[mock.call("cash_position"), mock.text("You have 2.65 lakh.")]
    )
    assert (await provider.complete([])).tool_calls[0].name == "cash_position"
    assert (await provider.complete([])).text == "You have 2.65 lakh."


async def test_mock_refuses_to_invent_a_figure():
    provider = mock.MockProvider()
    completion = await provider.complete(
        [Message(role="user", content="roughly what did we sell last year?")]
    )
    assert not completion.wants_tools
    assert "will not state a figure" in completion.text


async def test_mock_summarises_a_tool_result_instead_of_looping():
    provider = mock.MockProvider()
    completion = await provider.complete(
        [
            Message(role="user", content="cash position?"),
            Message(role="tool", tool_call_id="c1", content="Cash and bank: 265000.00"),
        ]
    )
    assert completion.text == "Cash and bank: 265000.00"
    assert not completion.wants_tools
