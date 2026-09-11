"""Meta WhatsApp Cloud API: signature verification, parsing, media, replies.

Everything that touches the wire lives here so the webhook route stays a thin
piece of routing. The provider is swappable - see ``twilio_whatsapp`` for the
shape another one takes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from tallyagent_core.errors import NotConfiguredError

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"
#: Anything larger is almost certainly not an invoice, and we will not spend a
#: multimodal call on it.
MAX_MEDIA_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class WhatsAppConfig:
    enabled: bool = False
    provider: str = "meta"
    verify_token: str = ""
    app_secret: str = ""
    access_token: str = ""
    phone_number_id: str = ""
    #: E.164 number -> company name. A number not in here is refused.
    allowlist: dict[str, str] = field(default_factory=dict)

    def company_for(self, number: str) -> str | None:
        return self.allowlist.get(normalise_number(number))


def normalise_number(raw: str) -> str:
    """Meta sends ``919876543210``; people configure ``+91 98765 43210``."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Check the ``X-Hub-Signature-256`` HMAC.

    An unsigned webhook is an open door to the whole approval queue, so this is
    checked before the body is even parsed as JSON.
    """
    if not app_secret:
        raise NotConfiguredError(
            "WHATSAPP_APP_SECRET is not set. The WhatsApp webhook refuses to "
            "accept requests it cannot authenticate."
        )
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def sign(body: bytes, app_secret: str) -> str:
    """Produce the header Meta would send. Used by tests and by the docs."""
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def verify_challenge(
    mode: str, token: str, challenge: str, config: WhatsAppConfig
) -> str | None:
    """The GET handshake Meta performs when you register the webhook."""
    if mode == "subscribe" and token and token == config.verify_token:
        return challenge
    return None


@dataclass(slots=True)
class InboundMessage:
    """One message, normalised away from Meta's envelope."""

    from_number: str
    message_id: str
    kind: str  # text | image | document | audio | unsupported
    text: str = ""
    media_id: str = ""
    mime_type: str = ""
    filename: str = ""

    @property
    def is_media(self) -> bool:
        return self.kind in ("image", "document")


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    """Pull messages out of Meta's deeply nested webhook body.

    Status callbacks (delivered/read) arrive on the same endpoint and carry no
    ``messages`` key; they are simply not messages and produce an empty list.
    """
    messages: list[InboundMessage] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            for raw in value.get("messages", []) or []:
                kind = raw.get("type", "")
                message = InboundMessage(
                    from_number=normalise_number(raw.get("from", "")),
                    message_id=raw.get("id", ""),
                    kind=kind if kind in ("text", "image", "document", "audio") else "unsupported",
                )
                if kind == "text":
                    message.text = (raw.get("text") or {}).get("body", "")
                elif kind in ("image", "document"):
                    media = raw.get(kind) or {}
                    message.media_id = media.get("id", "")
                    message.mime_type = media.get("mime_type", "")
                    message.filename = media.get("filename", "")
                    message.text = media.get("caption", "")
                messages.append(message)
    return messages


class MetaClient:
    """Outbound calls: fetch media, send replies."""

    def __init__(
        self,
        config: WhatsAppConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = GRAPH_BASE,
    ) -> None:
        self.config = config
        self.base_url = base_url.rstrip("/")
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=30.0,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.config.access_token}"},
        )

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Two hops: id -> signed URL -> bytes. Returns (data, mime type)."""
        async with self._client() as http:
            meta = await http.get(f"{self.base_url}/{media_id}")
            meta.raise_for_status()
            info = meta.json()
            url = info.get("url", "")
            mime = info.get("mime_type", "application/octet-stream")
            if not url:
                raise ValueError(f"media {media_id} has no download URL")

            data = await http.get(url)
            data.raise_for_status()
            content = data.content

        if len(content) > MAX_MEDIA_BYTES:
            raise ValueError(
                f"media {media_id} is {len(content)} bytes, over the "
                f"{MAX_MEDIA_BYTES} byte limit"
            )
        return content, mime

    async def send_text(self, to: str, body: str) -> dict[str, Any]:
        """WhatsApp truncates long bodies; do it ourselves so the cut is clean."""
        if len(body) > 4000:
            body = body[:3900] + "\n\n[...truncated. Open the tallyagent window for the rest.]"
        payload = {
            "messaging_product": "whatsapp",
            "to": normalise_number(to).removeprefix("+"),
            "type": "text",
            "text": {"body": body},
        }
        async with self._client() as http:
            response = await http.post(
                f"{self.base_url}/{self.config.phone_number_id}/messages",
                content=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            log.error("WhatsApp send failed: %s %s", response.status_code, response.text[:200])
        return {"status_code": response.status_code}
