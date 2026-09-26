"""Score the bill reader against bills whose correct answer is known.

    uv run python scripts/eval_bills.py --folder tests/fixtures/bill_pile
    uv run python scripts/eval_bills.py --folder demo/bills_real --label "real layouts"

Why this exists: the batch queues what it is confident about, and a partner
approves it. That gate holds - nothing posts unapproved - but "we queue it and a
human checks" is not an answer to "how often is it right", and a firm deciding
whether to trust a pile of forty drafts is asking the second question.

What it measures, per field, over one folder:

- **read** - the field came back at all;
- **right** - it matches the truth beside the scan;
- **queued and wrong** - the number that actually matters. A bill the reader was
  confident enough to draft, with a field a partner would have had to catch. A
  bill sent to the attention list is not in this count: correctly saying "I
  cannot read this" is the reader working, not failing.

The truth file is ``bill-001.json`` beside ``bill-001.png``, in the shape the
extractor returns. ``scripts/make_bill_pile.py`` writes one per bill it draws;
for real scans it is written once by hand.

Model calls cost money, so a run is cached by content: re-running after a change
to the scoring re-reads nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tallyagent_core import dotenv
from tallyagent_daemon import config as config_mod
from tallyagent_daemon import wiring
from tallyagent_tools import bills

log = logging.getLogger(__name__)

#: The fields a purchase voucher cannot be drafted without, and the ones a
#: partner would have to catch if they were wrong. Line item descriptions are
#: left out on purpose: they become a narration, not a number.
FIELDS = (
    "vendor",
    "invoice_no",
    "invoice_date",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "total",
)

#: Money read off a scan is right if it is right to the rupee. Half a paisa of
#: float noise in a truth file written as JSON is not a misread.
MONEY_TOLERANCE = Decimal("0.50")

MONEY_FIELDS = ("taxable_value", "cgst", "sgst", "igst", "total")


@dataclass(slots=True)
class FieldScore:
    read: int = 0
    right: int = 0
    wrong: int = 0

    @property
    def accuracy(self) -> float:
        return (self.right / self.read * 100) if self.read else 0.0


@dataclass(slots=True)
class BillScore:
    name: str
    status: str = ""
    seconds: float = 0.0
    wrong: dict[str, tuple[str, str]] = field(default_factory=dict)
    error: str = ""
    #: This scan cannot be read, and the only right answer is to say so.
    unreadable: bool = False

    @property
    def clean(self) -> bool:
        return not self.wrong and not self.error


@dataclass(slots=True)
class Report:
    label: str
    folder: str
    bills: list[BillScore] = field(default_factory=list)
    fields: dict[str, FieldScore] = field(default_factory=dict)

    @property
    def read_count(self) -> int:
        return len([b for b in self.bills if not b.error])

    @property
    def queued_wrong(self) -> list[BillScore]:
        """Drafted, and something a partner would have had to catch.

        Includes a scan that cannot be read but was drafted from anyway: a
        reader that invents figures for an unreadable document is worse than
        one that refuses it.
        """
        return [
            b
            for b in self.bills
            if b.status == bills.QUEUED and (b.wrong or b.unreadable)
        ]

    @property
    def attention(self) -> list[BillScore]:
        return [b for b in self.bills if b.status != bills.QUEUED]

    def markdown(self) -> str:
        total = len(self.bills)
        drafted = len([b for b in self.bills if b.status == bills.QUEUED])
        wrong = len(self.queued_wrong)
        lines = [
            f"# Bill reading accuracy - {self.label}",
            "",
            f"{total} bill(s) from `{self.folder}`. "
            f"{drafted} drafted, {len(self.attention)} sent for a person to look at, "
            f"**{wrong} drafted with something wrong**.",
            "",
            "A bill on the attention list is not counted as wrong: correctly "
            "saying it cannot be read is the reader working.",
            "",
            "| field | read | right | accuracy |",
            "|---|---|---|---|",
        ]
        for name in FIELDS:
            score = self.fields.get(name, FieldScore())
            lines.append(
                f"| {name} | {score.read}/{total} | {score.right} "
                f"| {score.accuracy:.0f}% |"
            )
        lines.append("")

        if self.queued_wrong:
            lines += ["## Drafted with something wrong", ""]
            for bill in self.queued_wrong:
                for name, (got, expected) in sorted(bill.wrong.items()):
                    lines.append(
                        f"- `{bill.name}` {name}: read {got!r}, should be {expected!r}"
                    )
            lines.append("")

        if self.attention:
            lines += ["## Sent for a person to look at", ""]
            for bill in self.attention:
                reason = bill.error or bill.status
                lines.append(f"- `{bill.name}`: {reason}")
            lines.append("")

        seconds = [b.seconds for b in self.bills if b.seconds]
        if seconds:
            lines += [
                f"Read in {sum(seconds):.0f}s, "
                f"{sum(seconds) / len(seconds):.1f}s per bill.",
                "",
            ]
        return "\n".join(lines)


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _letters(value: Any) -> str:
    """A name reduced to its letters and digits.

    Character recognition on a photographed heading loses the spaces:
    "BharatSupplies". The product resolves that to the ledger by the same rule
    (``masters.resolve_ledger_alias``), so scoring it as a miss would count a
    draft nobody has to correct - and what is being measured is what a partner
    would have to catch. A different name still has different letters.
    """
    return "".join(ch for ch in _text(value) if ch.isalnum())


def compare(got: dict[str, Any], truth: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Which fields disagree, as ``{field: (read, expected)}``.

    Kept a pure function so the scoring can be tested without reading a scan,
    which is the expensive part.
    """
    wrong: dict[str, tuple[str, str]] = {}
    for name in FIELDS:
        expected, actual = truth.get(name), got.get(name)
        if name in MONEY_FIELDS:
            want, have = _money(expected), _money(actual)
            if want is None:
                continue
            if have is None or abs(have - want) > MONEY_TOLERANCE:
                wrong[name] = (str(actual), str(expected))
            continue
        if name == "invoice_date":
            if _as_date(actual) != _as_date(expected):
                wrong[name] = (str(actual), str(expected))
            continue
        if name == "vendor":
            if _letters(expected) and _letters(actual) != _letters(expected):
                wrong[name] = (str(actual), str(expected))
            continue
        if _text(expected) and _text(actual) != _text(expected):
            wrong[name] = (str(actual), str(expected))
    return wrong


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def truth_for(path: Path) -> dict[str, Any] | None:
    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return None
    return json.loads(sidecar.read_text(encoding="utf-8"))


async def score(
    folder: str, label: str, limit: int = 0, cache_dir: str = "reports/bill_cache"
) -> Report:
    """Read every bill in a folder with the real chain and score the answers."""
    dotenv.load()
    config = config_mod.load("config/config.toml", "config/policy.toml")
    wired = wiring.build(config)
    ctx = wired.services.tools
    # Deliberately not the default chain: that tries the ``.json`` beside the
    # scan first, which here is the answer sheet. What is being measured is the
    # reader that runs on a folder nobody has touched - local OCR, then the
    # model on the text it produced.
    readers: list[Any] = []
    if bills._has_ocr():
        readers.append(bills.OcrExtractor(ctx.vision))
    readers.append(bills.ModelExtractor(ctx.vision))
    reader = bills.ChainExtractor(readers)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    report = Report(label=label, folder=folder)
    report.fields = {name: FieldScore() for name in FIELDS}

    for path in bills.find_bills(folder, limit):
        truth = truth_for(path)
        if truth is None:
            log.warning("no truth beside %s; skipping", path.name)
            continue

        outcome = BillScore(name=path.name)
        cached = cache / f"{bills.fingerprint(path)}.json"
        started = time.monotonic()
        try:
            if cached.is_file():
                got = json.loads(cached.read_text(encoding="utf-8"))
            else:
                got = await reader.extract(path)
                cached.write_text(json.dumps(got, default=str, indent=2), "utf-8")
            outcome.seconds = time.monotonic() - started
            outcome.status = bills.QUEUED
            if truth.get("_unreadable"):
                outcome.unreadable = True
                outcome.error = "drafted from a scan that cannot be read"
                report.bills.append(outcome)
                print(f"  {path.name:<18} {outcome.seconds:5.1f}s  {outcome.error}")
                continue
            outcome.wrong = compare(got, truth)
            for name in FIELDS:
                # A zero is a read field, not a missing one: on an inter-state
                # bill, CGST and SGST are genuinely nil and reading them as nil
                # is correct.
                if name in got and got[name] not in (None, ""):
                    report.fields[name].read += 1
                    if name in outcome.wrong:
                        report.fields[name].wrong += 1
                    else:
                        report.fields[name].right += 1
        except Exception as exc:  # noqa: BLE001 - an unreadable scan is a result
            outcome.status = bills.ATTENTION
            outcome.error = (
                "refused, correctly" if truth.get("_unreadable") else str(exc)[:120]
            )
            outcome.seconds = time.monotonic() - started

        report.bills.append(outcome)
        state = "ok" if outcome.clean else (outcome.error or ", ".join(outcome.wrong))
        print(f"  {path.name:<18} {outcome.seconds:5.1f}s  {state[:80]}")

    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="tests/fixtures/bill_pile")
    parser.add_argument("--label", default="generated pile")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report", default="reports/bill_accuracy.md")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    report = await score(args.folder, args.label, args.limit)
    if not report.bills:
        print(
            f"No bills with a truth file in {args.folder}. Each scan needs its "
            "correct answer beside it as JSON.",
        )
        return 1

    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.markdown(), encoding="utf-8")
    print()
    print(report.markdown())
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
