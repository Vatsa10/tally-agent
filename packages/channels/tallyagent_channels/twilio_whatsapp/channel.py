"""Twilio WhatsApp adapter skeleton.

Exists to keep the WhatsApp provider pluggable and to record what implementing
it involves. It fails loudly rather than half-working: a messaging channel that
silently drops a client's invoice is worse than one that is plainly absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_MESSAGE = (
    "The Twilio WhatsApp provider is not implemented. Set "
    '[channels.whatsapp] provider = "meta" in config.toml. To implement this, '
    "mirror tallyagent_channels.whatsapp.meta: Twilio signs webhooks with "
    "X-Twilio-Signature (HMAC-SHA1 over the URL plus sorted POST parameters, "
    "not the raw body), posts form-encoded fields rather than JSON, and serves "
    "media from MediaUrl0..N behind basic auth instead of a two-hop media id."
)


@dataclass(slots=True)
class TwilioConfig:
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    allowlist: dict[str, str] = field(default_factory=dict)


class TwilioWhatsAppClient:
    provider = "twilio"

    def __init__(self, config: TwilioConfig | None = None) -> None:
        self.config = config or TwilioConfig()

    def verify_signature(self, *args: object, **kwargs: object) -> bool:
        raise NotImplementedError(f"verify_signature: {_MESSAGE}")

    async def download_media(self, media_url: str) -> tuple[bytes, str]:
        raise NotImplementedError(f"download_media: {_MESSAGE}")

    async def send_text(self, to: str, body: str) -> dict[str, object]:
        raise NotImplementedError(f"send_text: {_MESSAGE}")
