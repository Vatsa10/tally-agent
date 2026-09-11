"""Stage 2: XML builders/parsers, quirks, client and backend against fake Tally."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from lxml import etree

from tallyagent_backends.zoho_books.backend import ZohoBooksBackend
from tallyagent_core.errors import ConnectionError_
from tallyagent_core.idempotency import make_key
from tallyagent_core.models import Ledger, Party, Voucher, VoucherLine, VoucherType
from tallyagent_tally.client import TallyClient, TallyConfig
from tallyagent_tally.xml import builders, parsers, quirks

# --- quirks -----------------------------------------------------------------


def test_dates_go_out_as_yyyymmdd():
    assert quirks.to_tally_date(date(2026, 6, 15)) == "20260615"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20260615", date(2026, 6, 15)),
        ("15-Jun-2026", date(2026, 6, 15)),
        ("2026-06-15", date(2026, 6, 15)),
        ("", None),
        ("-", None),
    ],
)
def test_incoming_dates_are_lenient(raw, expected):
    assert quirks.from_tally_date(raw) == expected


def test_amount_sign_convention_is_inverted_for_tally():
    # Our debit (+) becomes Tally's negative amount with ISDEEMEDPOSITIVE=Yes.
    assert quirks.tally_amount(Decimal("100.00")) == ("-100.00", "Yes")
    assert quirks.tally_amount(Decimal("-100.00")) == ("100.00", "No")


def test_clean_response_survives_real_tally_garbage():
    dirty = (
        b"<ENVELOPE><BODY><DATA><COLLECTION><LEDGER>"
        b"<NAME>Acme\x03 &#4;Industries</NAME>"
        b"<UDF:NOTE>x</UDF:NOTE>"
        b"</LEDGER></COLLECTION></DATA></BODY></ENVELOPE>"
    )
    cleaned = quirks.clean_response(dirty)
    assert b'xmlns:UDF="TallyUDF"' in cleaned
    root = etree.fromstring(cleaned)
    assert root.findtext(".//NAME") == "Acme Industries"


def test_educational_mode_date_restriction_is_detected():
    assert quirks.educational_mode_blocks(date(2026, 6, 15))
    assert not quirks.educational_mode_blocks(date(2026, 6, 1))
    assert not quirks.educational_mode_blocks(date(2026, 7, 31))


# --- builders ---------------------------------------------------------------


def test_export_collection_envelope_shape():
    root = etree.fromstring(
        builders.build_export_collection("List of Ledgers", company="Demo Co")
    )
    assert root.findtext("HEADER/TALLYREQUEST") == "Export"
    assert root.findtext("HEADER/TYPE") == "Collection"
    assert root.findtext("HEADER/ID") == "List of Ledgers"
    assert root.findtext(".//SVCURRENTCOMPANY") == "Demo Co"
    assert root.findtext(".//SVEXPORTFORMAT") == "$$SysName:XML"


def test_report_name_alias_is_applied():
    root = etree.fromstring(builders.build_export_report("Profit and Loss A/c"))
    assert root.findtext("HEADER/ID") == "Profit and Loss"


def test_voucher_element_uses_tally_signs_and_no_objview(sales_voucher: Voucher):
    element = builders.build_voucher_element(sales_voucher)
    assert "OBJVIEW" not in element.attrib
    assert element.findtext("DATE") == "20260615"
    assert element.get("VCHTYPE") == "Sales"

    entries = element.findall("ALLLEDGERENTRIES.LIST")
    debit = entries[0]
    assert debit.findtext("LEDGERNAME") == "Acme Industries"
    assert debit.findtext("ISDEEMEDPOSITIVE") == "Yes"
    assert debit.findtext("AMOUNT") == "-11800.00"

    credit = entries[1]
    assert credit.findtext("ISDEEMEDPOSITIVE") == "No"
    assert credit.findtext("AMOUNT") == "10000.00"


def test_special_characters_are_escaped_not_concatenated():
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 15),
        narration='Smith & Co <"adjustment">',
        lines=[
            VoucherLine(ledger_name="Smith & Co", amount=Decimal("1")),
            VoucherLine(ledger_name="Cash", amount=Decimal("-1")),
        ],
    )
    xml = builders.to_string(builders.build_voucher_element(voucher))
    assert "&amp;" in xml and "&lt;" in xml
    # Round-trips: the escaping is correct, not merely present.
    root = etree.fromstring(xml.encode())
    assert root.findtext("NARRATION") == 'Smith & Co <"adjustment">'


def test_alter_carries_the_master_id():
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 15),
        master_id="42",
        lines=[
            VoucherLine(ledger_name="Cash", amount=Decimal("1")),
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("-1")),
        ],
    )
    element = builders.build_voucher_element(voucher, action="Alter", remote_id="42")
    assert element.get("ACTION") == "Alter"
    assert element.get("MASTERID") == "42"
    assert element.findtext("MASTERID") == "42"


def test_ledger_and_party_elements():
    ledger = builders.build_ledger_element(
        Ledger(name="New Bank", parent="Bank Accounts", opening_balance=Decimal("500"))
    )
    assert ledger.findtext("PARENT") == "Bank Accounts"
    assert ledger.findtext("OPENINGBALANCE") == "500"

    party = builders.build_party_element(
        Party(name="Zed Ltd", gstin="27AAPFU0939F1ZV", credit_period_days=30)
    )
    assert party.findtext("PARENT") == "Sundry Debtors"
    assert party.findtext("PARTYGSTIN") == "27AAPFU0939F1ZV"
    assert party.findtext("BILLCREDITPERIOD") == "30 Days"
    assert party.findtext("ISBILLWISEON") == "Yes"


def test_import_envelope_wraps_elements():
    element = etree.Element("LEDGER")
    root = etree.fromstring(builders.build_import([element], "All Masters", "Demo Co"))
    assert root.findtext("HEADER/TALLYREQUEST") == "Import Data"
    assert root.findtext(".//REPORTNAME") == "All Masters"
    assert root.findtext(".//SVCURRENTCOMPANY") == "Demo Co"
    assert root.find(".//TALLYMESSAGE/LEDGER") is not None


# --- parsers ----------------------------------------------------------------


def test_line_errors_are_classified():
    xml = (
        b"<ENVELOPE><BODY><DATA><IMPORTRESULT>"
        b"<CREATED>0</CREATED><ALTERED>0</ALTERED><EXCEPTIONS>2</EXCEPTIONS>"
        b"<LINEERROR>Ledger 'Acme Industires' does not exist in the company</LINEERROR>"
        b"<LINEERROR>Duplicate voucher number 'S/1'</LINEERROR>"
        b"</IMPORTRESULT></DATA></BODY></ENVELOPE>"
    )
    result = parsers.parse_import_result(xml)
    assert not result.ok
    assert [e.kind for e in result.errors] == ["unknown_master", "duplicate"]
    assert result.errors[0].missing_master == "Acme Industires"


def test_exceptions_without_lineerror_fall_back_to_lasterror():
    xml = (
        b"<ENVELOPE><BODY><DATA><IMPORTRESULT>"
        b"<CREATED>0</CREATED><EXCEPTIONS>1</EXCEPTIONS>"
        b"<LASTERROR>Company is not open</LASTERROR>"
        b"</IMPORTRESULT></DATA></BODY></ENVELOPE>"
    )
    result = parsers.parse_import_result(xml)
    assert result.messages == ["Company is not open"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234.50", Decimal("1234.50")), ("(-)500", Decimal("-500")), ("", Decimal("0")),
     (None, Decimal("0")), ("junk", Decimal("0"))],
)
def test_amount_parsing_is_total(raw, expected):
    assert parsers.to_decimal(raw) == expected


# --- client + fake server ---------------------------------------------------


async def test_probe_detects_version_and_company(backend):
    info = await backend.probe()
    assert info["status"] == "ok"
    assert info["version"] == "6.0.3"
    assert "Demo Traders Pvt Ltd" in info["companies"]


async def test_unreachable_tally_raises_a_useful_connection_error():
    client = TallyClient(TallyConfig(host="0.0.0.0", port=1, timeout_seconds=0.2))
    with pytest.raises(ConnectionError_, match="Cannot connect to Tally"):
        await client.list_companies()


async def test_get_masters_maps_parties_and_state_codes(backend):
    masters = await backend.get_masters()
    assert "Sales - GST 18%" in masters.ledger_names
    acme = next(p for p in masters.parties if p.name == "Acme Industries")
    assert acme.is_customer
    assert acme.state_code == "27"
    bharat = next(p for p in masters.parties if p.name == "Bharat Supplies")
    assert not bharat.is_customer
    assert bharat.state_code == "29"


async def test_create_voucher_posts_and_moves_the_trial_balance(
    backend, fake_tally, sales_voucher
):
    key = make_key("Demo Traders Pvt Ltd", sales_voucher)
    result = await backend.create_voucher(sales_voucher, key)
    assert result.ok, result.errors
    assert result.master_id
    assert "<VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>" in result.raw_request

    assert fake_tally.balance("Acme Industries") == Decimal("11800.00")
    assert fake_tally.balance("Sales - GST 18%") == Decimal("-10000.00")

    tb = await backend.trial_balance()
    assert tb["Output CGST"] == Decimal("-900.00")


async def test_replaying_the_same_voucher_is_a_no_op(backend, fake_tally, sales_voucher):
    key = make_key("Demo Traders Pvt Ltd", sales_voucher)
    first = await backend.create_voucher(sales_voucher, key)
    assert first.ok and not first.replayed

    second = await backend.create_voucher(sales_voucher, key)
    assert second.replayed
    assert second.ok
    assert second.master_id == first.master_id
    assert len(fake_tally.vouchers) == 1


async def test_unknown_ledger_comes_back_as_a_structured_error(backend, fake_tally):
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 15),
        lines=[
            VoucherLine(ledger_name="Nonexistent Ledger", amount=Decimal("100")),
            VoucherLine(ledger_name="Cash", amount=Decimal("-100")),
        ],
    )
    result = await backend.create_voucher(voucher, make_key("Demo", voucher))
    assert not result.ok
    assert "does not exist" in result.errors[0]
    assert fake_tally.vouchers == []


async def test_a_failed_write_may_be_retried_after_the_ledger_is_created(
    backend, fake_tally
):
    voucher = Voucher(
        voucher_type=VoucherType.JOURNAL,
        date=date(2026, 6, 15),
        lines=[
            VoucherLine(ledger_name="Rent", amount=Decimal("100")),
            VoucherLine(ledger_name="Cash", amount=Decimal("-100")),
        ],
    )
    key = make_key("Demo", voucher)
    assert not (await backend.create_voucher(voucher, key)).ok

    await backend.create_ledger(
        Ledger(name="Rent", parent="Indirect Expenses"), make_key("Demo-ledger", voucher)
    )
    retry = await backend.create_voucher(voucher, key)
    assert retry.ok
    assert fake_tally.balance("Rent") == Decimal("100")


async def test_alter_voucher_by_master_id(backend, fake_tally, sales_voucher):
    created = await backend.create_voucher(
        sales_voucher, make_key("Demo", sales_voucher)
    )
    amended = sales_voucher.model_copy(update={"narration": "Corrected narration"})
    result = await backend.alter_voucher(
        amended, created.master_id, make_key("Demo", amended, salt="alter")
    )
    assert result.ok, result.errors
    assert fake_tally.vouchers[0].narration == "Corrected narration"


async def test_alter_with_an_unknown_master_id_fails_loudly(backend, sales_voucher):
    """Tally does not reject an unmatched amendment - it silently creates a new
    voucher. That is worse than an error, so the adapter turns it into one."""
    result = await backend.alter_voucher(
        sales_voucher, "999999", make_key("Demo", sales_voucher, salt="alter")
    )
    assert not result.ok
    assert "did not match an existing voucher" in result.errors[0]
    assert "duplicate must be removed" in result.errors[0]


async def test_alter_without_an_id_never_reaches_tally(backend, fake_tally, sales_voucher):
    """An empty id is the same trap, caught before the envelope is even built."""
    before = len(fake_tally.requests)
    result = await backend.alter_voucher(
        sales_voucher, "", make_key("Demo", sales_voucher, salt="blank")
    )
    assert not result.ok
    assert "find_voucher" in result.errors[0]
    assert len(fake_tally.requests) == before, "nothing may be sent"

    zero = await backend.alter_voucher(
        sales_voucher, "0", make_key("Demo", sales_voucher, salt="zero")
    )
    assert not zero.ok


async def test_a_voucher_id_of_zero_is_treated_as_absent():
    """Tally returns LASTMID=0 on a voucher import; taking it literally hands
    "0" to an alter, which then duplicates."""
    xml = (
        b"<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED>"
        b"<LASTVCHID>9</LASTVCHID><LASTMID>0</LASTMID>"
        b"<ERRORS>0</ERRORS><EXCEPTIONS>0</EXCEPTIONS></RESPONSE>"
    )
    result = parsers.parse_import_result(xml)
    assert result.ok
    assert result.last_voucher_id == "9"
    assert result.last_master_id == "", "0 means no master id, not master id 0"


async def test_an_errors_counter_is_a_failure_even_with_no_lineerror():
    xml = (
        b"<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED>"
        b"<ERRORS>2</ERRORS><EXCEPTIONS>0</EXCEPTIONS></RESPONSE>"
    )
    result = parsers.parse_import_result(xml)
    assert not result.ok
    assert "2 error(s)" in result.messages[0]


async def test_find_vouchers_returns_ids_an_amendment_can_use(
    backend, fake_tally, sales_voucher
):
    await backend.create_voucher(sales_voucher, make_key("Demo", sales_voucher))
    found = await backend.find_vouchers(reference="INV-001")
    assert len(found) == 1
    assert found[0]["voucher_type"] == "Sales"
    assert found[0]["master_id"], "an amendment needs an id it can match on"

    assert await backend.find_vouchers(reference="NOPE") == []


async def test_outstanding_receivables_and_ageing(backend, sales_voucher):
    await backend.create_voucher(sales_voucher, make_key("Demo", sales_voucher))
    bills = await backend.get_outstanding(receivable=True, as_on=date(2026, 8, 1))
    acme = next(b for b in bills if b.party_name == "Acme Industries")
    assert acme.amount == Decimal("11800.00")
    assert acme.overdue_days == 47
    assert acme.ageing_bucket() == "31-60"


async def test_bank_ledger_entries_are_filtered_by_ledger_and_date(backend, fake_tally):
    voucher = Voucher(
        voucher_type=VoucherType.RECEIPT,
        date=date(2026, 6, 20),
        narration="Cheque 4411",
        lines=[
            VoucherLine(ledger_name="Bank - HDFC 1234", amount=Decimal("5000")),
            VoucherLine(ledger_name="Acme Industries", amount=Decimal("-5000")),
        ],
    )
    await backend.create_voucher(voucher, make_key("Demo", voucher))
    entries = await backend.get_bank_ledger(
        "Bank - HDFC 1234", date(2026, 6, 1), date(2026, 6, 30)
    )
    assert len(entries) == 1
    assert entries[0].amount == Decimal("5000")
    assert entries[0].narration == "Cheque 4411"

    outside = await backend.get_bank_ledger(
        "Bank - HDFC 1234", date(2026, 7, 1), date(2026, 7, 31)
    )
    assert outside == []


# --- backend boundary -------------------------------------------------------


def test_tally_backend_satisfies_the_protocol(backend):
    from tallyagent_backends.accounting_backend import AccountingBackend

    assert isinstance(backend, AccountingBackend)


async def test_zoho_skeleton_fails_loudly_with_a_useful_message():
    zoho = ZohoBooksBackend()
    with pytest.raises(NotImplementedError, match="AccountingBackend protocol"):
        await zoho.list_companies()
