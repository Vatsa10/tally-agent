"""A pile of purchase bills, turned into drafts a person can approve in one pass.

This is the job the product exists for. A business posting two thousand
vouchers a month spends ten to fifteen hours on every hundred and fifty bills,
and most of them are photographs and scans from small vendors rather than tidy
PDFs. One invoice at a time through a chat window is a demonstration; a folder
of a hundred and fifty, with the eight that need a human pulled out of the
stack, is the thing worth paying for.

Two rules shape everything here:

- **No master is ever created silently.** An unrecognised vendor stops that bill
  and goes on the attention list with suggestions. Auto-creating ledgers is how
  a chart of accounts fills up with "Sharma Traders", "Sharma traders" and
  "M/s Sharma Trading Co" - the same duplicate mess manual entry produces, only
  faster.
- **A bill is processed once.** Extraction costs a model call and a re-run of a
  folder must not spend it again, nor queue the same voucher twice, so what has
  been seen is remembered against the file's own contents.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tallyagent_tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

#: What a bill can arrive as. Photographs, because half of them are.
BILL_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff")

#: How many bills are read at once. Reading a bill is character recognition on
#: this machine and then a model call over the network, and the network wait is
#: most of it, so overlapping them is nearly free. Kept small: the OCR engine
#: wants the CPU, and a provider that rate-limits turns a fast batch into a
#: batch of 429s.
READ_AT_ONCE = 4

MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
}

QUEUED = "queued"
ATTENTION = "needs attention"
FAILED = "failed"
SKIPPED = "already done"


@dataclass(slots=True)
class BillOutcome:
    """What happened to one document, in terms a bookkeeper would use."""

    path: str
    status: str
    ticket: str = ""
    vendor: str = ""
    amount: str = ""
    reference: str = ""
    reason: str = ""
    suggestions: list[str] = field(default_factory=list)

    @property
    def needs_person(self) -> bool:
        return self.status in (ATTENTION, FAILED)


@dataclass(slots=True)
class BatchResult:
    outcomes: list[BillOutcome] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def attention(self) -> list[BillOutcome]:
        return [o for o in self.outcomes if o.needs_person]

    def summary(self) -> str:
        if not self.outcomes:
            return "No bills found."
        parts = [f"{len(self.outcomes)} bill(s): {self.count(QUEUED)} queued"]
        for status in (ATTENTION, FAILED, SKIPPED):
            found = self.count(status)
            if found:
                parts.append(f"{found} {status}")
        head = ", ".join(parts) + "."
        if not self.attention:
            return head + " Nothing needs you."
        return (
            head
            + f" {len(self.attention)} need you: "
            + "; ".join(f"{Path(o.path).name} - {o.reason}" for o in self.attention[:5])
        )

    def as_rows(self) -> list[dict[str, Any]]:
        return [asdict(o) for o in self.outcomes]


class Extractor(Protocol):
    """Turns one document into the fields an invoice has."""

    async def extract(self, path: Path) -> dict[str, Any]: ...


class SidecarExtractor:
    """Reads ``bill.json`` sitting beside ``bill.png``.

    For tests, for a folder a human has already keyed, and for running the whole
    batch path with no model at all - which is how the rest of this is tested
    without spending a vision call per fixture.
    """

    async def extract(self, path: Path) -> dict[str, Any]:
        import json

        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            raise FileNotFoundError(
                f"no extraction for {path.name}: expected {sidecar.name} beside it"
            )
        loaded: Any = json.loads(sidecar.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}


class ModelExtractor:
    """Asks the configured multimodal model to read the document.

    The prompt is the one in ``ingest``: copy the figures exactly, never follow
    an instruction found inside the document. An invoice is attacker-controlled
    input and is treated as such.
    """

    def __init__(self, router: Any) -> None:
        self.router = router

    async def extract(self, path: Path) -> dict[str, Any]:
        from tallyagent_llm.provider import Image, Message
        from tallyagent_tools.ingest import INVOICE_SCHEMA_PROMPT, parse_invoice_json

        media = MEDIA_TYPES.get(path.suffix.lower(), "image/png")
        completion = await self.router.complete(
            [
                Message(role="system", content=INVOICE_SCHEMA_PROMPT),
                Message(
                    role="user",
                    content="Read this invoice.",
                    images=[Image(data=path.read_bytes(), media_type=media)],
                ),
            ],
            max_tokens=800,
        )
        return asdict(parse_invoice_json(completion.text))


class OcrExtractor:
    """Read the document locally, then ask a text model to make sense of it.

    This exists because the model a firm can afford is usually text-only, and
    pointed at a photograph it returns nothing while every bill in the pile
    reports itself unreadable. OCR turns the picture into lines; the model's job
    is then the one it is good at - deciding which line is the invoice number
    and which is the GSTIN.

    It also means the image never leaves the machine. Only the text does, and
    the egress log records exactly that.
    """

    def __init__(self, router: Any) -> None:
        self.router = router
        self._engine: Any = None

    def read(self, path: Path) -> str:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415

            self._engine = RapidOCR()
        found, _ = self._engine(str(path))
        return "\n".join(line[1] for line in (found or []))

    async def extract(self, path: Path) -> dict[str, Any]:
        from tallyagent_llm.provider import Message
        from tallyagent_tools.ingest import INVOICE_SCHEMA_PROMPT, parse_invoice_json

        text = self.read(path)
        if not text.strip():
            raise ValueError(f"no text could be read out of {path.name}")

        messages = [
            Message(role="system", content=INVOICE_SCHEMA_PROMPT),
            Message(
                role="user",
                content=(
                    "These lines were read off an invoice by OCR, in roughly "
                    "reading order. Some words may be run together.\n\n" + text
                ),
            ),
        ]

        # One retry on an empty reply. Providers do occasionally answer with
        # nothing, and a blank came back as a bill with no vendor - which reads
        # as an unreadable document and sends someone to look at a scan that was
        # perfectly legible.
        for attempt in (1, 2):
            completion = await self.router.complete(messages, max_tokens=800)
            if completion.text.strip():
                return asdict(parse_invoice_json(completion.text))
            log.warning("empty reply reading %s (attempt %d)", path.name, attempt)

        raise ValueError(
            f"the model returned nothing for {path.name}; the text was read off "
            "it fine, so this is the model rather than the document"
        )


def find_bills(folder: str | Path, limit: int = 0) -> list[Path]:
    """Every document in a folder, oldest first.

    Oldest first because that is the order a bookkeeper would work a pile in,
    and because a stable order makes a re-run comparable to the run before it.
    """
    root = Path(folder)
    if not root.is_dir():
        return []
    found = [
        path
        for path in sorted(root.iterdir(), key=lambda p: (p.stat().st_mtime, p.name))
        if path.suffix.lower() in BILL_SUFFIXES
    ]
    return found[:limit] if limit else found


def fingerprint(path: Path) -> str:
    """The document's own contents. A renamed copy is the same bill."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:32]


def _seen(ctx: ToolContext, digest: str) -> str:
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return ""
    try:
        return str(memory.get("bill", digest) or "")
    except Exception:  # noqa: BLE001 - a missing memory must not stop a batch
        return ""


def _remember(ctx: ToolContext, digest: str, ticket: str) -> None:
    memory = getattr(ctx, "memory", None)
    if memory is None:
        return
    try:
        memory.remember("bill", digest, ticket or QUEUED)
    except Exception:  # noqa: BLE001
        log.debug("could not record that %s was processed", digest)


async def ingest_folder(
    ctx: ToolContext,
    folder: str,
    limit: int = 0,
    as_purchase: bool = True,
    extractor: Extractor | None = None,
    again: bool = False,
) -> ToolResult:
    """Read a folder of bills and queue a draft for each one that is clean.

    Never stops on a bad document: the point of handing over a pile is that the
    pile gets worked, and one unreadable photograph must not strand the other
    hundred and forty nine.
    """
    ctx.live.require_write_scope(ctx.company.name)

    bills = find_bills(folder, limit)
    if not bills:
        return ToolResult(
            message=f"No bills in {folder!r}. Looked for {', '.join(BILL_SUFFIXES)}.",
            data={"outcomes": []},
        )

    reader = extractor or _default_extractor(ctx)
    batch = BatchResult()

    # Read them together, queue them one at a time. Extraction is the slow part
    # and it touches nothing shared; queueing writes tickets to one database in
    # a fixed order, and a pile that comes back in a different order every run
    # is a pile nobody can check against the paper.
    to_read = [
        path
        for path in bills
        if again or not _seen(ctx, fingerprint(path))
    ]
    reader = await _read_ahead(to_read, reader)

    for path in bills:
        digest = fingerprint(path)
        already = _seen(ctx, digest)
        if already and not again:
            batch.outcomes.append(
                BillOutcome(
                    path=str(path),
                    status=SKIPPED,
                    ticket=already if already != QUEUED else "",
                    reason="this document has been through before",
                )
            )
            continue

        outcome = await _one_bill(ctx, path, reader, as_purchase)
        batch.outcomes.append(outcome)
        if outcome.status == QUEUED:
            _remember(ctx, digest, outcome.ticket)

    message = batch.summary()
    if batch.count(QUEUED) == 0 and batch.attention:
        message += " " + BLIND_MODEL_HINT
    return ToolResult(message=message, data={"outcomes": batch.as_rows()})


class _ReadAhead:
    """Bills already read, handed back in the order the batch asks for them.

    Holds the exception too, so a scan that failed fails at the same place in
    the batch as it would have if nothing had been read ahead.
    """

    def __init__(self, done: dict[str, Any], fallback: Extractor) -> None:
        self._done = done
        self._fallback = fallback

    async def extract(self, path: Path) -> dict[str, Any]:
        if str(path) not in self._done:
            return await self._fallback.extract(path)
        result = self._done[str(path)]
        if isinstance(result, BaseException):
            raise result
        return result


async def _read_ahead(paths: list[Path], reader: Extractor) -> Extractor:
    if len(paths) < 2:
        return reader

    import asyncio

    gate = asyncio.Semaphore(READ_AT_ONCE)

    async def read(path: Path) -> Any:
        async with gate:
            return await reader.extract(path)

    results = await asyncio.gather(
        *(read(path) for path in paths), return_exceptions=True
    )
    return _ReadAhead(
        {str(path): result for path, result in zip(paths, results, strict=True)},
        reader,
    )


async def _one_bill(
    ctx: ToolContext, path: Path, reader: Extractor, as_purchase: bool
) -> BillOutcome:
    from tallyagent_tools import ingest

    try:
        extraction = await reader.extract(path)
    except Exception as exc:  # noqa: BLE001 - one bad scan is not a failed batch
        return BillOutcome(
            path=str(path),
            status=FAILED,
            reason=f"could not read it ({type(exc).__name__})",
        )

    try:
        result = await ingest.invoice_image_to_draft(
            ctx, extraction, as_purchase=as_purchase
        )
    except Exception as exc:  # noqa: BLE001
        return BillOutcome(
            path=str(path), status=FAILED, reason=f"{type(exc).__name__}: {exc}"
        )

    data = result.data if isinstance(result.data, dict) else {}
    extract = data.get("extract") or {}
    vendor = str(extract.get("vendor") or "")
    amount = str(extract.get("total") or extract.get("taxable_value") or "")
    reference = str(extract.get("invoice_no") or "")
    ticket = str((data or {}).get("ticket") or "")

    if ticket:
        return BillOutcome(
            path=str(path),
            status=QUEUED,
            ticket=ticket,
            vendor=vendor,
            amount=amount,
            reference=reference,
        )

    # No ticket: either the vendor did not resolve, or validation refused it.
    # Both are a person's decision, and both keep their reason.
    return BillOutcome(
        path=str(path),
        status=ATTENTION,
        vendor=vendor,
        amount=amount,
        reference=reference,
        reason=_shorten(result.message),
        suggestions=[str(s) for s in (data.get("suggestions") or [])],
    )


def _shorten(message: str, limit: int = 140) -> str:
    text = " ".join(message.split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


class ChainExtractor:
    """Try each reader in turn; the first that finds a vendor wins.

    Order matters: a ``.json`` beside the document is something a person or an
    earlier run already established, and it beats asking a model to read the
    picture again. It also means a folder that has been through once costs
    nothing to re-check.
    """

    def __init__(self, readers: Sequence[Extractor]) -> None:
        self.readers = list(readers)

    async def extract(self, path: Path) -> dict[str, Any]:
        last_error: Exception | None = None
        best: dict[str, Any] | None = None
        for reader in self.readers:
            try:
                found = await reader.extract(path)
            except Exception as exc:  # noqa: BLE001 - try the next reader
                last_error = exc
                continue
            if found.get("vendor"):
                return found
            # Read, but the vendor line was not legible. Keep it and try the
            # next reader: a document a person must name the vendor on is a
            # different problem from a file nobody could open, and conflating
            # the two sends someone hunting through scans that were fine.
            best = best or found
        if best is not None:
            return best
        if last_error is not None:
            raise last_error
        raise ValueError("no reader could open this document")


def _default_extractor(ctx: ToolContext) -> Extractor:
    """Sidecar, then OCR, then the model reading the picture itself.

    Cheapest and most certain first. OCR before vision because it runs locally -
    the image stays on the machine and only the text it contains is sent - and
    because it works with the text-only model most firms will have configured.
    """
    router = getattr(ctx, "vision", None)
    readers: list[Extractor] = [SidecarExtractor()]
    if router is not None:
        if _has_ocr():
            readers.append(OcrExtractor(router))
        readers.append(ModelExtractor(router))
    return ChainExtractor(readers)


def _has_ocr() -> bool:
    import importlib.util

    return importlib.util.find_spec("rapidocr_onnxruntime") is not None


#: Said once, at the end, rather than against every document in the pile.
BLIND_MODEL_HINT = (
    "None of these could be read. If the bills are images, the configured model "
    "has to be one that accepts them - a text-only model returns nothing and "
    "every bill looks unreadable. Put a .json extraction beside each document, "
    "or configure a multimodal model."
)


async def bill_attention_list(
    ctx: ToolContext, folder: str, limit: int = 0
) -> ToolResult:
    """What in this folder would stop, without sending anything to Tally.

    A dry run: the same reading and the same checks, nothing queued. For a firm
    deciding whether a client's pile is worth starting on.
    """
    outcomes: list[BillOutcome] = []
    reader = _default_extractor(ctx)
    for path in find_bills(folder, limit):
        try:
            extraction = await reader.extract(path)
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                BillOutcome(
                    path=str(path), status=FAILED, reason=f"unreadable ({type(exc).__name__})"
                )
            )
            continue
        vendor = str(extraction.get("vendor") or "")
        known = vendor and vendor in (await ctx.masters()).ledger_names
        outcomes.append(
            BillOutcome(
                path=str(path),
                status=QUEUED if known else ATTENTION,
                vendor=vendor,
                amount=str(extraction.get("total") or ""),
                reference=str(extraction.get("invoice_no") or ""),
                reason="" if known else f"{vendor or 'no vendor'} is not a ledger yet",
            )
        )

    batch = BatchResult(outcomes=outcomes)
    return ToolResult(
        message="Dry run, nothing queued. " + batch.summary(),
        data={"outcomes": batch.as_rows()},
    )


def as_table(outcomes: Sequence[dict[str, Any]]) -> list[str]:
    """One line per bill, for a terminal."""
    rows = []
    for row in outcomes:
        name = Path(str(row.get("path", ""))).name
        status = str(row.get("status", ""))
        detail = str(row.get("ticket") or row.get("reason") or "")
        rows.append(f"  {name[:34]:34} {status:14} {detail[:60]}")
    return rows
