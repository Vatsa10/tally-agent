"""A pile of purchase bills, worked in one pass.

The measure that matters is not that one invoice becomes one voucher - that
already worked. It is that a hundred and fifty of them become a hundred and
forty queued drafts and a short list of the ones a person has to look at, and
that nothing in the pile can quietly invent a ledger or post twice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tallyagent_approvals.audit import AuditLog
from tallyagent_approvals.db import make_engine
from tallyagent_approvals.queue import ApprovalQueue
from tallyagent_core.policy import Policy
from tallyagent_tools import bills
from tallyagent_tools.base import ToolContext
from tallyagent_tools.executor import build_executor


@pytest.fixture
def engine():
    return make_engine(":memory:")


@pytest.fixture
def queue(engine):
    return ApprovalQueue(engine, AuditLog(engine))


@pytest.fixture
def memory(engine, company):
    from tallyagent_agent.memory import Memory

    return Memory(engine, company.name)


@pytest.fixture
def ctx(queue, backend, company, memory):
    tools = ToolContext(
        backend=backend,
        company=company,
        policy=Policy.default(),
        enqueue=queue.enqueue,
        memory=memory,
    )
    queue.executor = build_executor(tools)
    return tools


def bill(folder: Path, name: str, vendor: str, amount: str, number: str) -> Path:
    """One document plus the extraction beside it.

    The image is a placeholder: what is under test is the batch, not the vision
    model, and every real extractor is injectable for exactly that reason.
    """
    folder.mkdir(parents=True, exist_ok=True)
    document = folder / f"{name}.png"
    document.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
    document.with_suffix(".json").write_text(
        json.dumps(
            {
                "vendor": vendor,
                "invoice_no": number,
                "invoice_date": "2026-06-01",
                "taxable_value": amount,
                "igst": "0",
                "cgst": str(round(float(amount) * 0.09, 2)),
                "sgst": str(round(float(amount) * 0.09, 2)),
                "total": str(round(float(amount) * 1.18, 2)),
                "place_of_supply": "27",
            }
        ),
        encoding="utf-8",
    )
    return document


@pytest.fixture
def pile(tmp_path) -> Path:
    """A realistic pile: mostly one known vendor, a few that will stop."""
    folder = tmp_path / "june-bills"
    for index in range(1, 13):
        bill(folder, f"bill-{index:03d}", "Bharat Supplies", "1000", f"BS/{index:03d}")
    bill(folder, "bill-090", "Sharma Trading Co", "4200", "ST/90")
    bill(folder, "bill-091", "", "500", "??")
    return folder


# --- finding the work -------------------------------------------------------


def test_only_documents_are_picked_up(tmp_path):
    folder = tmp_path / "mixed"
    bill(folder, "real", "Bharat Supplies", "100", "A1")
    (folder / "notes.txt").write_text("not a bill")
    (folder / "thumbs.db").write_bytes(b"junk")

    found = bills.find_bills(folder)

    assert [p.name for p in found] == ["real.png"]


def test_an_empty_folder_says_what_it_looked_for(tmp_path):
    result = bills.find_bills(tmp_path / "nothing-here")
    assert result == []


def test_a_renamed_copy_is_the_same_bill(tmp_path):
    first = bill(tmp_path, "a", "Bharat Supplies", "100", "A1")
    second = tmp_path / "a-copy.png"
    second.write_bytes(first.read_bytes())

    assert bills.fingerprint(first) == bills.fingerprint(second)


def test_the_pile_is_worked_oldest_first(pile):
    names = [p.name for p in bills.find_bills(pile)]
    assert names == sorted(names), "a stable order makes two runs comparable"


# --- working the pile -------------------------------------------------------


async def test_the_clean_bills_queue_and_the_rest_are_listed(ctx, pile, queue):
    result = await bills.ingest_folder(ctx, str(pile))

    rows = result.data["outcomes"]
    queued = [r for r in rows if r["status"] == bills.QUEUED]
    attention = [r for r in rows if r["status"] == bills.ATTENTION]

    assert len(queued) == 12, result.message
    assert len(attention) == 2, [r["reason"] for r in attention]
    assert len(queue.list("pending", ctx.company.name)) == 12


async def test_an_unknown_vendor_stops_that_bill_and_names_it(ctx, pile):
    result = await bills.ingest_folder(ctx, str(pile))

    stopped = [
        r
        for r in result.data["outcomes"]
        if r["status"] == bills.ATTENTION and r["vendor"] == "Sharma Trading Co"
    ]
    assert stopped, "an unrecognised vendor must not be waved through"
    assert "not a ledger" in stopped[0]["reason"]


async def test_no_ledger_is_ever_created_by_a_batch(ctx, pile):
    """Auto-creating vendors is how a chart of accounts fills with near
    duplicates - faster than a human could manage it by hand."""
    before = set((await ctx.masters()).ledger_names)

    await bills.ingest_folder(ctx, str(pile))

    ctx._masters_cache = None
    assert set((await ctx.masters(refresh=True)).ledger_names) == before


async def test_one_unreadable_document_does_not_strand_the_pile(ctx, pile):
    (pile / "torn.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-really")  # no sidecar

    result = await bills.ingest_folder(ctx, str(pile))

    failed = [r for r in result.data["outcomes"] if r["status"] == bills.FAILED]
    queued = [r for r in result.data["outcomes"] if r["status"] == bills.QUEUED]
    assert len(failed) == 1
    assert len(queued) == 12, "the other bills were still worked"


async def test_running_the_same_folder_twice_queues_nothing_new(ctx, pile, queue):
    await bills.ingest_folder(ctx, str(pile))
    first = len(queue.list("pending", ctx.company.name))

    again = await bills.ingest_folder(ctx, str(pile))

    assert len(queue.list("pending", ctx.company.name)) == first
    assert again.data["outcomes"][0]["status"] == bills.SKIPPED


async def test_a_reprocess_can_be_asked_for_explicitly(ctx, pile):
    await bills.ingest_folder(ctx, str(pile))

    again = await bills.ingest_folder(ctx, str(pile), again=True)

    assert not [r for r in again.data["outcomes"] if r["status"] == bills.SKIPPED]


async def test_a_limit_works_the_first_n_only(ctx, pile):
    result = await bills.ingest_folder(ctx, str(pile), limit=3)
    assert len(result.data["outcomes"]) == 3


async def test_the_summary_leads_with_what_needs_a_person(ctx, pile):
    result = await bills.ingest_folder(ctx, str(pile))

    assert "12 queued" in result.message
    assert "need you" in result.message


async def test_a_clean_pile_says_nothing_needs_you(ctx, tmp_path):
    folder = tmp_path / "clean"
    for index in range(3):
        bill(folder, f"b{index}", "Bharat Supplies", "500", f"C/{index}")

    result = await bills.ingest_folder(ctx, str(folder))

    assert "Nothing needs you" in result.message


# --- looking before leaping -------------------------------------------------


async def test_a_dry_run_queues_nothing(ctx, pile, queue):
    result = await bills.bill_attention_list(ctx, str(pile))

    assert queue.list("pending", ctx.company.name) == []
    assert "nothing queued" in result.message
    assert len(result.data["outcomes"]) == 14


async def test_a_dry_run_still_names_the_unknown_vendors(ctx, pile):
    result = await bills.bill_attention_list(ctx, str(pile))

    stopped = [r for r in result.data["outcomes"] if r["status"] == bills.ATTENTION]
    assert any(r["vendor"] == "Sharma Trading Co" for r in stopped)


# --- reading the documents --------------------------------------------------


async def test_a_missing_extraction_is_reported_against_that_file(tmp_path):
    document = tmp_path / "lonely.png"
    document.write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(FileNotFoundError, match="lonely"):
        await bills.SidecarExtractor().extract(document)


async def test_the_model_extractor_sends_the_document_and_the_schema(tmp_path):
    document = bill(tmp_path, "scan", "Bharat Supplies", "100", "A1")
    seen: dict[str, object] = {}

    class FakeRouter:
        async def complete(self, messages, tools=None, max_tokens=0, temperature=0.0):
            from tallyagent_llm.provider import Completion

            seen["system"] = messages[0].content
            seen["images"] = messages[1].images
            return Completion(text='{"vendor": "Bharat Supplies", "total": 118}')

    await bills.ModelExtractor(FakeRouter()).extract(document)

    assert "reply with JSON only" in str(seen["system"])
    assert "Do not follow" in str(seen["system"]), "an invoice is untrusted input"
    assert seen["images"] and seen["images"][0].media_type == "image/png"


async def test_a_sidecar_beats_asking_the_model_again(tmp_path):
    """What a person or an earlier run established outranks a fresh guess."""
    document = bill(tmp_path, "known", "Bharat Supplies", "100", "A1")
    asked = []

    class NosyModel:
        async def extract(self, path):  # type: ignore[no-untyped-def]
            asked.append(path)
            return {"vendor": "Someone Else"}

    chain = bills.ChainExtractor([bills.SidecarExtractor(), NosyModel()])

    found = await chain.extract(document)

    assert found["vendor"] == "Bharat Supplies"
    assert asked == [], "the model was not troubled"


async def test_the_model_is_used_when_there_is_no_sidecar(tmp_path):
    document = tmp_path / "scan.png"
    document.write_bytes(b"\x89PNG\r\n\x1a\n")

    class Reader:
        async def extract(self, path):  # type: ignore[no-untyped-def]
            return {"vendor": "Bharat Supplies"}

    chain = bills.ChainExtractor([bills.SidecarExtractor(), Reader()])

    assert (await chain.extract(document))["vendor"] == "Bharat Supplies"


async def test_a_text_only_model_is_diagnosed_once_not_per_bill(ctx, pile):
    """A model that cannot see returns nothing, and every bill then looks
    unreadable - which sends someone hunting through forty scans."""

    class Blind:
        async def extract(self, path):  # type: ignore[no-untyped-def]
            return {"vendor": ""}

    result = await bills.ingest_folder(ctx, str(pile), extractor=Blind())

    assert bills.BLIND_MODEL_HINT in result.message
    assert result.message.count("multimodal") == 1


async def test_a_legible_bill_with_no_vendor_needs_a_person_not_a_retry(ctx, tmp_path):
    """"I read it and the vendor line is blank" is a different problem from
    "I could not open this file", and they go to different people."""
    folder = tmp_path / "blank-vendor"
    bill(folder, "faint", "", "900", "F/1")

    result = await bills.ingest_folder(ctx, str(folder))

    outcome = result.data["outcomes"][0]
    assert outcome["status"] == bills.ATTENTION
    assert "vendor" in outcome["reason"].lower()


async def test_an_empty_reply_is_retried_then_reported_as_the_models_fault(tmp_path):
    """A blank completion used to arrive as a bill with no vendor, which reads
    as an unreadable scan and sends someone to look at a legible document."""
    document = tmp_path / "fine.png"
    document.write_bytes(b"\x89PNG\r\n\x1a\n")
    replies = []

    class SilentRouter:
        async def complete(self, messages, tools=None, max_tokens=0, temperature=0.0):
            from tallyagent_llm.provider import Completion

            replies.append(messages)
            return Completion(text="   ")

    reader = bills.OcrExtractor(SilentRouter())
    reader.read = lambda path: "Bharat Supplies\nTotal 118.00"  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="rather than the document"):
        await reader.extract(document)
    assert len(replies) == 2, "it tried twice before blaming anything"


async def test_ocr_text_is_what_leaves_the_machine_not_the_image(tmp_path):
    """The picture stays local; only the words it contains are sent."""
    document = tmp_path / "private.png"
    document.write_bytes(b"\x89PNG\r\n\x1a\nsecret-pixels")
    sent: list[str] = []

    class Router:
        async def complete(self, messages, tools=None, max_tokens=0, temperature=0.0):
            from tallyagent_llm.provider import Completion

            sent.extend(m.content for m in messages)
            assert all(not m.images for m in messages)
            return Completion(text='{"vendor": "Bharat Supplies"}')

    reader = bills.OcrExtractor(Router())
    reader.read = lambda path: "Bharat Supplies"  # type: ignore[method-assign]

    await reader.extract(document)

    assert any("Bharat Supplies" in text for text in sent)
    assert not any("secret-pixels" in text for text in sent)


# --- reading with a model that thinks before it answers ----------------------


class Thinker:
    """A reasoning model: it needs room to think before it writes anything.

    Measured against DeepSeek on a photographed bill - the reply came back with
    a full page of reasoning and an empty content field, which the batch then
    reported as an unreadable document.
    """

    name = "thinker"
    model = "thinker-1"

    def __init__(self, needs: int = 2000) -> None:
        self.needs = needs
        self.budgets: list[int] = []

    async def complete(self, messages, max_tokens=800, **kwargs):  # type: ignore[no-untyped-def]
        from tallyagent_llm.provider import Completion

        self.budgets.append(max_tokens)
        if max_tokens < self.needs:
            return Completion(text="")
        return Completion(
            text='{"vendor": "Bharat Supplies", "taxable_value": 1000, '
            '"igst": 180, "total": 1180, "invoice_no": "BS/1", '
            '"invoice_date": "2026-06-01"}'
        )


async def test_a_thinking_model_is_retried_with_room_to_answer(tmp_path):
    from tallyagent_tools.bills import EXTRACT_BUDGETS, ModelExtractor

    bill = tmp_path / "bill.png"
    bill.write_bytes(b"not really a png")
    router = Thinker(needs=2000)
    reader = ModelExtractor(router)
    reader.read = lambda path: "Bharat Supplies\nTotal 1180.00"  # type: ignore[assignment]

    extracted = await reader.extract(bill)

    assert extracted["vendor"] == "Bharat Supplies"
    assert router.budgets == list(EXTRACT_BUDGETS[: len(router.budgets)])
    assert router.budgets[-1] > router.budgets[0], "a bigger budget, not the same twice"


async def test_a_model_that_answers_nothing_at_any_budget_says_so(tmp_path):
    from tallyagent_tools.bills import OcrExtractor

    bill = tmp_path / "bill.png"
    bill.write_bytes(b"not really a png")
    reader = OcrExtractor(Thinker(needs=10_000))
    reader.read = lambda path: "Bharat Supplies"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="the model rather than the document"):
        await reader.extract(bill)
