"""Provider selection, cost accounting and the egress log.

Two jobs. First, build the configured provider - config decides, nothing else.
Second, wrap every call so that what left the machine is recorded: destination,
bytes, which fields, tokens and cost. "Tally data stays local" is only a claim
unless it is measured.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tallyagent_core.errors import NotConfiguredError
from tallyagent_llm.provider import Completion, Message, Provider

log = logging.getLogger(__name__)

#: USD per 1M tokens (input, output). Rough list prices, used for a running
#: cost estimate only - it is not a billing system, and stale entries simply
#: make the estimate less useful, never wrong in a way that affects behaviour.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "deepseek-flash": (Decimal("0.28"), Decimal("0.42")),
    "deepseek-chat": (Decimal("0.27"), Decimal("1.10")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "mock-deterministic": (Decimal("0"), Decimal("0")),
}

ENV_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@dataclass(slots=True)
class ModelConfig:
    provider: str = "deepseek"
    model: str = "deepseek-flash"
    base_url: str = ""
    timeout: float = 120.0
    max_steps: int = 12


@dataclass(slots=True)
class EgressRecord:
    """One model request, as it will appear in the egress log."""

    destination: str
    provider: str
    model: str
    bytes_sent: int
    fields: list[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: Decimal = Decimal("0")


def api_key_for(provider: str, explicit: str = "") -> str:
    """Secrets come from the environment or the OS keyring, never config files."""
    if explicit:
        return explicit
    env_name = ENV_KEYS.get(provider, "")
    key = os.environ.get(env_name, "") if env_name else ""
    if key:
        return key
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring.get_password("tallyagent", provider) or ""
    except Exception:  # noqa: BLE001 - keyring is optional and may not be installed
        return ""


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Decimal:
    prices = PRICES.get(model)
    if prices is None:
        return Decimal("0")
    inp, out = prices
    million = Decimal("1000000")
    return (
        Decimal(prompt_tokens) / million * inp
        + Decimal(completion_tokens) / million * out
    ).quantize(Decimal("0.000001"))


def build_provider(
    config: ModelConfig, api_key: str = "", **kwargs: Any
) -> Provider:
    """Instantiate the configured provider.

    Falling back to the mock when a key is missing is deliberate and loud: a CA
    firm running the demo without a key gets a working, obviously-fake agent
    rather than a stack trace.
    """
    name = config.provider.lower()
    if name == "mock":
        from tallyagent_llm.mock import MockProvider

        return MockProvider(**kwargs)

    if name not in ENV_KEYS:
        # Reject an unknown name before asking for its key, so the error names
        # the real problem rather than a missing variable nobody has heard of.
        raise NotConfiguredError(
            f"unknown model provider {config.provider!r}. "
            f"Known: mock, {', '.join(sorted(ENV_KEYS))}."
        )

    key = api_key_for(name, api_key)
    if not key:
        raise NotConfiguredError(
            f"no API key for provider {name!r}. Set {ENV_KEYS.get(name, 'the API key')} "
            "in the environment or the OS keyring, or set "
            '[model] provider = "mock" in config.toml.'
        )

    if name == "deepseek":
        from tallyagent_llm.deepseek import DEFAULT_BASE_URL, DeepSeekProvider

        return DeepSeekProvider(
            key, config.model, config.base_url or DEFAULT_BASE_URL, config.timeout, **kwargs
        )
    if name == "anthropic":
        from tallyagent_llm.anthropic_ import DEFAULT_BASE_URL, AnthropicProvider

        return AnthropicProvider(
            key, config.model, config.base_url or DEFAULT_BASE_URL, config.timeout, **kwargs
        )
    if name == "openai":
        from tallyagent_llm.openai_ import DEFAULT_BASE_URL, OpenAIProvider

        return OpenAIProvider(
            key, config.model, config.base_url or DEFAULT_BASE_URL, config.timeout, **kwargs
        )
    raise AssertionError(f"provider {name!r} is in ENV_KEYS but has no constructor")


class Router:
    """Wraps a provider with accounting. The agent talks only to this."""

    def __init__(
        self,
        provider: Provider,
        destination: str = "",
        on_egress: Any = None,
    ) -> None:
        self.provider = provider
        self.destination = destination or _destination_for(provider)
        self.on_egress = on_egress
        self.egress: list[EgressRecord] = []

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    @property
    def total_cost(self) -> Decimal:
        return sum((record.cost_usd for record in self.egress), Decimal("0"))

    @property
    def total_tokens(self) -> int:
        return sum(r.prompt_tokens + r.completion_tokens for r in self.egress)

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> Completion:
        completion = await self.provider.complete(
            messages, tools, max_tokens=max_tokens, temperature=temperature
        )
        record = EgressRecord(
            destination=self.destination,
            provider=self.provider.name,
            model=completion.model or self.provider.model,
            bytes_sent=completion.raw_bytes_sent,
            fields=_fields_sent(messages),
            prompt_tokens=completion.usage.prompt_tokens,
            completion_tokens=completion.usage.completion_tokens,
            cached_tokens=completion.usage.cached_tokens,
            cost_usd=estimate_cost(
                completion.model or self.provider.model,
                completion.usage.prompt_tokens,
                completion.usage.completion_tokens,
            ),
        )
        self.egress.append(record)
        log.info(
            "egress: %d bytes to %s (%s), fields=%s, tokens=%d/%d, cost~$%s",
            record.bytes_sent,
            record.destination,
            record.model,
            ",".join(record.fields),
            record.prompt_tokens,
            record.completion_tokens,
            record.cost_usd,
        )
        if self.on_egress is not None:
            self.on_egress(record)
        return completion


def _destination_for(provider: Provider) -> str:
    base = getattr(provider, "base_url", "")
    return base or f"local:{provider.name}"


#: Message roles and shapes, summarised for the egress log. Deliberately not the
#: content: the log must be safe to keep and to show a client.
def _fields_sent(messages: list[Message]) -> list[str]:
    fields: list[str] = []
    for message in messages:
        label = message.role
        if message.images:
            label += f"+{len(message.images)}image"
        if message.tool_calls:
            label += "+toolcall"
        fields.append(label)
    return fields
