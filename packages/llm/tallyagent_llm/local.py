"""A model running inside the firm: Ollama, LM Studio, vLLM - anything that
speaks the OpenAI chat API on the firm's own machine or network.

The research behind this is blunt: a CA's duty of confidentiality is personal,
ICAI is building its own "sovereign" model because of exactly this worry, and a
cloud-only default is the objection most likely to end a sale. With this, no
byte of a client's books leaves the building.

So the one rule that matters here is that "local" has to be true. An endpoint
that is not on this machine or a private network is refused at start-up: a
"local" model that is really an internet host would make the egress log say
nothing left the machine when something did.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from tallyagent_core.errors import NotConfiguredError
from tallyagent_llm.deepseek import DeepSeekProvider

#: Ollama's OpenAI-compatible endpoint, the most common way a firm would run one.
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen2.5:14b-instruct"

#: Hostnames that are local by definition, without asking DNS.
LOCAL_NAMES = {"localhost", "host.docker.internal"}


def is_private(base_url: str) -> bool:
    """Is this endpoint on this machine or the firm's private network?

    Resolves the name, because ``ollama.office`` is fine if it is 192.168.1.20
    and not fine if it is a public address. Anything that cannot be resolved is
    treated as not private - the safe direction.
    """
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return False
    if host in LOCAL_NAMES:
        return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    if not addresses:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(str(address).split("%")[0])
        if not (ip.is_loopback or ip.is_private or ip.is_link_local):
            return False
    return True


class LocalProvider(DeepSeekProvider):
    """The OpenAI-compatible transport, pointed at the firm's own hardware.

    Inherits the transport rather than copying it, the same as the OpenAI
    provider: one fix to tool-call parsing lands everywhere.
    """

    name = "local"

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        check_private: bool = True,
    ) -> None:
        if check_private and not is_private(base_url):
            raise NotConfiguredError(
                f"refusing to use {base_url} as a local model: it is not on this "
                "machine or a private network. A local model is one whose traffic "
                "never leaves the firm; for a hosted one, use provider = "
                '"openai" with its base_url, so the egress log tells the truth.'
            )
        # Local servers ignore the key; the transport still sends a header.
        super().__init__(api_key or "local", model, base_url, timeout, transport)
