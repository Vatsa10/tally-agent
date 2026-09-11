"""Tally XML request builders.

Envelope shapes taken from vendor/tally_mcp_pypi/tally_mcp/xml_builder.py (MIT)
and re-expressed with lxml element construction end-to-end. Nothing here does
string concatenation of user data: lxml escapes text and attributes by
construction, which is the difference between a ledger named ``Smith & Co`` and
a malformed request.
"""

from __future__ import annotations

from decimal import Decimal

from lxml import etree

from tallyagent_core.models import Ledger, Party, Voucher
from tallyagent_tally.xml.quirks import (
    REPORT_NAME_ALIASES,
    tally_amount,
    to_tally_date,
)


def _envelope(tally_request: str, type_: str, ident: str) -> tuple[etree._Element, etree._Element]:
    envelope = etree.Element("ENVELOPE")
    header = etree.SubElement(envelope, "HEADER")
    etree.SubElement(header, "VERSION").text = "1"
    etree.SubElement(header, "TALLYREQUEST").text = tally_request
    etree.SubElement(header, "TYPE").text = type_
    etree.SubElement(header, "ID").text = ident
    body = etree.SubElement(envelope, "BODY")
    return envelope, body


def _static_variables(
    desc: etree._Element,
    company: str,
    username: str = "",
    password: str = "",
    from_date: str = "",
    to_date: str = "",
    extra: dict[str, str] | None = None,
) -> None:
    sv = etree.SubElement(desc, "STATICVARIABLES")
    if company:
        etree.SubElement(sv, "SVCURRENTCOMPANY").text = company
    etree.SubElement(sv, "SVEXPORTFORMAT").text = "$$SysName:XML"
    if username:
        etree.SubElement(sv, "SVUSERNAME").text = username
    if password:
        etree.SubElement(sv, "SVPASSWORD").text = password
    if from_date:
        etree.SubElement(sv, "SVFROMDATE").text = from_date
    if to_date:
        etree.SubElement(sv, "SVTODATE").text = to_date
    for key, value in (extra or {}).items():
        etree.SubElement(sv, key).text = value


def _serialise(envelope: etree._Element) -> bytes:
    return etree.tostring(envelope, xml_declaration=False)


def _attach_tdl(desc: etree._Element, tdl: str) -> None:
    if not tdl:
        return
    tdl_elem = etree.SubElement(desc, "TDL")
    tdl_msg = etree.SubElement(tdl_elem, "TDLMESSAGE")
    for child in etree.fromstring(f"<ROOT>{tdl}</ROOT>"):
        tdl_msg.append(child)


# --- exports ----------------------------------------------------------------


def build_export_collection(
    collection_name: str,
    company: str = "",
    username: str = "",
    password: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
    tdl: str = "",
) -> bytes:
    envelope, body = _envelope("Export", "Collection", collection_name)
    desc = etree.SubElement(body, "DESC")
    _static_variables(desc, company, username, password, from_date, to_date, extra_vars)
    _attach_tdl(desc, tdl)
    return _serialise(envelope)


def build_export_report(
    report_name: str,
    company: str = "",
    username: str = "",
    password: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
    tdl: str = "",
) -> bytes:
    envelope, body = _envelope(
        "Export", "Data", REPORT_NAME_ALIASES.get(report_name, report_name)
    )
    desc = etree.SubElement(body, "DESC")
    _static_variables(desc, company, username, password, from_date, to_date, extra_vars)
    _attach_tdl(desc, tdl)
    return _serialise(envelope)


# --- imports ----------------------------------------------------------------


def build_import(
    payload: list[etree._Element],
    request_type: str,
    company: str,
    username: str = "",
    password: str = "",
) -> bytes:
    """Wrap already-built master/voucher elements in an import envelope.

    ``request_type`` is Tally's report name for the import: ``Vouchers`` or
    ``All Masters``.
    """
    envelope = etree.Element("ENVELOPE")
    header = etree.SubElement(envelope, "HEADER")
    etree.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = etree.SubElement(envelope, "BODY")
    importdata = etree.SubElement(body, "IMPORTDATA")

    requestdesc = etree.SubElement(importdata, "REQUESTDESC")
    etree.SubElement(requestdesc, "REPORTNAME").text = request_type
    sv = etree.SubElement(requestdesc, "STATICVARIABLES")
    etree.SubElement(sv, "SVCURRENTCOMPANY").text = company
    if username:
        etree.SubElement(sv, "SVUSERNAME").text = username
    if password:
        etree.SubElement(sv, "SVPASSWORD").text = password

    requestdata = etree.SubElement(importdata, "REQUESTDATA")
    tallymsg = etree.SubElement(requestdata, "TALLYMESSAGE", nsmap={"UDF": "TallyUDF"})
    for element in payload:
        tallymsg.append(element)
    return _serialise(envelope)


def build_voucher_element(
    voucher: Voucher,
    action: str = "Create",
    remote_id: str | None = None,
) -> etree._Element:
    """One ``<VOUCHER>`` element.

    ``action="Alter"`` plus ``remote_id`` (a MASTERID or GUID) is how an
    existing voucher is amended; Tally matches on the id, not on content.
    Deliberately emits no OBJVIEW attribute - see quirks.NEVER_EMIT_ATTRIBUTES.
    """
    attrib = {"VCHTYPE": voucher.voucher_type.value, "ACTION": action}
    if remote_id:
        attrib["REMOTEID" if len(remote_id) > 12 else "MASTERID"] = remote_id
    element = etree.Element("VOUCHER", attrib=attrib)

    etree.SubElement(element, "DATE").text = to_tally_date(voucher.date)
    etree.SubElement(element, "VOUCHERTYPENAME").text = voucher.voucher_type.value
    if voucher.voucher_number:
        etree.SubElement(element, "VOUCHERNUMBER").text = voucher.voucher_number
    if voucher.party_name:
        etree.SubElement(element, "PARTYLEDGERNAME").text = voucher.party_name
    if voucher.narration:
        etree.SubElement(element, "NARRATION").text = voucher.narration
    if voucher.reference:
        etree.SubElement(element, "REFERENCE").text = voucher.reference
    if action == "Alter" and voucher.master_id:
        etree.SubElement(element, "MASTERID").text = voucher.master_id
    if action == "Alter" and voucher.guid:
        etree.SubElement(element, "GUID").text = voucher.guid

    for line in voucher.lines:
        entry = etree.SubElement(element, "ALLLEDGERENTRIES.LIST")
        etree.SubElement(entry, "LEDGERNAME").text = line.ledger_name
        amount_text, deemed_positive = tally_amount(line.amount)
        etree.SubElement(entry, "ISDEEMEDPOSITIVE").text = deemed_positive
        etree.SubElement(entry, "AMOUNT").text = amount_text
        if line.bill_reference:
            bill = etree.SubElement(entry, "BILLALLOCATIONS.LIST")
            etree.SubElement(bill, "NAME").text = line.bill_reference
            etree.SubElement(bill, "BILLTYPE").text = "New Ref"
            etree.SubElement(bill, "AMOUNT").text = amount_text
        if line.cost_centre:
            cat = etree.SubElement(entry, "CATEGORYALLOCATIONS.LIST")
            etree.SubElement(cat, "CATEGORY").text = "Primary Cost Category"
            cc = etree.SubElement(cat, "COSTCENTREALLOCATIONS.LIST")
            etree.SubElement(cc, "NAME").text = line.cost_centre
            etree.SubElement(cc, "AMOUNT").text = amount_text
    return element


def build_ledger_element(ledger: Ledger, action: str = "Create") -> etree._Element:
    element = etree.Element("LEDGER", attrib={"NAME": ledger.name, "ACTION": action})
    etree.SubElement(element, "NAME").text = ledger.name
    etree.SubElement(element, "PARENT").text = ledger.parent
    if ledger.opening_balance != Decimal("0"):
        etree.SubElement(element, "OPENINGBALANCE").text = format(
            ledger.opening_balance, "f"
        )
    if ledger.gstin:
        etree.SubElement(element, "PARTYGSTIN").text = ledger.gstin
        etree.SubElement(element, "GSTREGISTRATIONTYPE").text = (
            ledger.gst_registration_type or "Regular"
        )
    if ledger.state:
        etree.SubElement(element, "LEDSTATENAME").text = ledger.state
    return element


def build_party_element(party: Party, action: str = "Create") -> etree._Element:
    """A party is a ledger under Sundry Debtors/Creditors plus trade terms.

    The state travels with it. Tally stores a state *name* while GST reasons in
    *codes*, so a party created without one has no resolvable place of supply,
    and every voucher for that party then warns instead of choosing IGST or
    CGST+SGST. Found against live Tally.
    """
    from tallyagent_tally.backend import STATE_NAMES

    state = None
    if party.state_code:
        state = STATE_NAMES.get(party.state_code.zfill(2))
    ledger = Ledger(
        name=party.ledger_name,
        parent="Sundry Debtors" if party.is_customer else "Sundry Creditors",
        gstin=party.gstin,
        state=state,
    )
    element = build_ledger_element(ledger, action=action)
    if party.credit_period_days:
        etree.SubElement(element, "BILLCREDITPERIOD").text = (
            f"{party.credit_period_days} Days"
        )
    etree.SubElement(element, "ISBILLWISEON").text = "Yes"
    return element


def to_string(element: etree._Element) -> str:
    """Human-readable XML, for the approval diff's "raw XML that will be sent"."""
    return etree.tostring(element, pretty_print=True, encoding="unicode")
