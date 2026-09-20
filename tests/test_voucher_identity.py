"""Finding a voucher we posted, later, from nothing but the books.

The handle that makes a voucher amendable is an id we choose at creation. For
a long time the belief was that Tally never gives it back, which made a local
table the only copy - lose it and the books become read-only to us.

Tally does give it back, in REMOTEGUID. The REMOTEID attribute looks like the
same field and carries Tally's own GUID instead, and an amendment aimed at that
is refused with "Voucher does not exist!" - which reads like the voucher is
missing rather than like the handle is wrong.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tallyagent_core.idempotency import make_key
from tallyagent_core.models import Voucher, VoucherLine, VoucherType


@pytest.fixture
def journal() -> Voucher:
    return Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 1),
        narration="identity",
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("11.00")),
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("-11.00")),
        ],
    )


async def _post(backend, voucher: Voucher, salt: str = "id") -> str:
    key = make_key("Demo", voucher, salt=salt)
    result = await backend.create_voucher(voucher, key)
    assert result.ok, result.errors
    return result.remote_id


async def test_the_id_we_assigned_comes_back_out_of_the_books(backend, journal):
    remote_id = await _post(backend, journal)

    rows = await backend.get_vouchers()
    ours = [r for r in rows if r.get("REMOTEID") == remote_id]

    assert ours, "the id we created the voucher with is not readable back"


async def test_tallys_own_guid_is_kept_but_never_mistaken_for_ours(backend, journal):
    """Both exist. Confusing them is the whole bug."""
    remote_id = await _post(backend, journal)

    row = next(r for r in await backend.get_vouchers() if r["REMOTEID"] == remote_id)

    assert row["TALLY_GUID"] != remote_id
    assert row["TALLY_GUID"], "Tally's own id is still there for anyone who needs it"


async def test_a_voucher_can_be_found_by_the_id_we_gave_it(backend, journal):
    remote_id = await _post(backend, journal)

    found = await backend.find_by_remote_id(remote_id)

    assert found is not None
    kind, when, master_id = found
    assert kind is VoucherType.JOURNAL
    assert when == date(2026, 6, 1)
    assert master_id


async def test_an_id_that_was_never_posted_is_simply_not_found(backend):
    assert await backend.find_by_remote_id("ta1_nothing") is None
    assert await backend.find_by_remote_id("") is None


async def test_deleting_uses_the_vouchers_own_date_not_the_callers(backend, journal):
    """Tally matches a delete on id, type and date together, and answers a
    mismatch with "Voucher does not exist!" - so a caller a day out looks like a
    caller with the wrong id. The date is taken from the books instead."""
    remote_id = await _post(backend, journal)

    result = await backend.delete_voucher(
        remote_id,
        VoucherType.JOURNAL,
        date(2026, 1, 1),  # deliberately wrong
        "delete-wrong-date",
    )

    assert result.ok, result.errors
    assert not await backend.voucher_exists(remote_id)


async def test_deleting_the_wrong_way_round_still_refuses(backend):
    result = await backend.delete_voucher(
        "", VoucherType.JOURNAL, date(2026, 6, 1), "delete-empty"
    )
    assert not result.ok
    assert "without the REMOTEID" in result.errors[0]


async def test_amending_works_from_the_books_alone(backend, journal):
    """No local index consulted anywhere in this test - only Tally."""
    remote_id = await _post(backend, journal)

    amended = journal.model_copy(update={"narration": "identity, corrected"})
    result = await backend.alter_voucher(amended, remote_id, "alter-from-books")

    assert result.ok, result.errors
    rows = [r for r in await backend.get_vouchers() if r["REMOTEID"] == remote_id]
    assert rows, "the voucher kept its id through the amendment"
    assert rows[0]["NARRATION"] == "identity, corrected"


async def test_amending_does_not_multiply_the_voucher(backend, fake_tally, journal):
    remote_id = await _post(backend, journal)
    before = len(fake_tally.vouchers)

    amended = journal.model_copy(update={"narration": "again"})
    await backend.alter_voucher(amended, remote_id, "alter-once")

    assert len(fake_tally.vouchers) == before
