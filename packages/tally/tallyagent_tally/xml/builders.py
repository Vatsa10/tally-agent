"""Tally XML request builders.

Envelope shapes taken from vendor/tally_mcp_pypi/tally_mcp/xml_builder.py (MIT)
and re-expressed with lxml element construction end-to-end. Nothing here does
string concatenation of user data: lxml escapes text and attributes by
construction, which is the difference between a ledger named ``Smith & Co`` and
a malformed request.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from lxml import etree

from tallyagent_core.models import (
    CostCategory,
    CostCentre,
    Godown,
    InventoryLine,
    Ledger,
    Party,
    StockGroup,
    StockItem,
    Unit,
    Voucher,
    VoucherLine,
    VoucherType,
)
from tallyagent_core.models.costing import PRIMARY_CATEGORY
from tallyagent_tally.xml.quirks import (
    REPORT_NAME_ALIASES,
    tally_amount,
    tally_quantity,
    tally_rate,
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

    ``remote_id`` is the voucher's identity **for the life of the voucher**, and
    it is set on Create, not just on Alter. That is the whole trick: TallyPrime
    1.1.7.1 will not amend or delete a voucher addressed by the MASTERID or GUID
    *it* assigned - ``ACTION="Alter"`` silently creates a duplicate instead,
    which is worse than failing. Address it by a REMOTEID we chose at creation
    and both work properly (measured: Alter gives altered=1 with the voucher
    count unchanged; Delete removes it).

    Deliberately emits no OBJVIEW attribute - see quirks.NEVER_EMIT_ATTRIBUTES.
    """
    attrib = {"VCHTYPE": voucher.voucher_type.value, "ACTION": action}
    if remote_id:
        attrib["REMOTEID"] = remote_id
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
    stock_line = _stock_bearing_line(voucher)

    for line in voucher.lines:
        entry = etree.SubElement(element, "ALLLEDGERENTRIES.LIST")
        etree.SubElement(entry, "LEDGERNAME").text = line.ledger_name
        amount_text, deemed_positive = tally_amount(line.amount)
        etree.SubElement(entry, "ISDEEMEDPOSITIVE").text = deemed_positive
        etree.SubElement(entry, "AMOUNT").text = amount_text
        if line.bill_reference:
            bill = etree.SubElement(entry, "BILLALLOCATIONS.LIST")
            etree.SubElement(bill, "NAME").text = line.bill_reference
            # A sale or a bill *raises* a reference; a receipt or payment
            # *settles* one. Marking a settlement "New Ref" makes Tally open a
            # second bill for the same invoice, and the original stays
            # outstanding for ever.
            etree.SubElement(bill, "BILLTYPE").text = (
                "Agst Ref"
                if voucher.voucher_type in (VoucherType.RECEIPT, VoucherType.PAYMENT)
                else "New Ref"
            )
            etree.SubElement(bill, "AMOUNT").text = amount_text
        if line.cost_centre:
            cat = etree.SubElement(entry, "CATEGORYALLOCATIONS.LIST")
            etree.SubElement(cat, "CATEGORY").text = (
                line.cost_category or PRIMARY_CATEGORY
            )
            etree.SubElement(cat, "ISDEEMEDPOSITIVE").text = deemed_positive
            cc = etree.SubElement(cat, "COSTCENTREALLOCATIONS.LIST")
            etree.SubElement(cc, "NAME").text = line.cost_centre
            etree.SubElement(cc, "AMOUNT").text = amount_text

        if line is stock_line:
            for item in voucher.inventory:
                _append_inventory_entry(entry, item, voucher.voucher_type)

    return element


def _stock_bearing_line(voucher: Voucher) -> VoucherLine | None:
    """Which ledger line the stock hangs off, if any.

    TallyPrime 1.1.7.1 rejects the item-invoice shape - inventory entries at
    voucher level - with a bare ``EXCEPTIONS 1`` and no message, whatever the
    signs, units or batch blocks. What it accepts is the voucher-mode shape:
    ``<INVENTORYALLOCATIONS.LIST>`` nested inside the *revenue* ledger entry,
    the one whose value the goods make up. Measured across nine payload shapes.

    The revenue line is found by value rather than by name, because "Sales - GST
    18%" is a convention and the ledger could be called anything.
    """
    if not voucher.inventory:
        return None
    value = voucher.inventory_value
    for line in voucher.lines:
        if abs(line.amount) == value and line.ledger_name != voucher.party_name:
            return line
    for line in voucher.lines:
        if line.ledger_name != voucher.party_name:
            return line
    return None


def _append_inventory_entry(
    element: etree._Element, item: InventoryLine, voucher_type: VoucherType
) -> None:
    """One ``<INVENTORYALLOCATIONS.LIST>``, nested in a ledger entry.

    ``element`` is the revenue ledger's ``<ALLLEDGERENTRIES.LIST>``, not the
    voucher - see ``_stock_bearing_line``. Amounts follow the same negation as
    ledger entries: stock going out of a sale is a credit to the trading
    account, so the sign is the mirror of the quantity's.
    """
    entry = etree.SubElement(element, "INVENTORYALLOCATIONS.LIST")
    etree.SubElement(entry, "STOCKITEMNAME").text = item.stock_item
    etree.SubElement(entry, "ISDEEMEDPOSITIVE").text = (
        "Yes" if item.is_inward else "No"
    )
    etree.SubElement(entry, "RATE").text = tally_rate(item.rate, item.unit)

    # Outward stock carries a positive amount in Tally's convention, inward a
    # negative one - the opposite of the quantity, exactly like a ledger line.
    signed = item.amount if not item.is_inward else -item.amount
    etree.SubElement(entry, "AMOUNT").text = format(signed, "f")
    etree.SubElement(entry, "ACTUALQTY").text = tally_quantity(
        item.quantity, item.unit
    )
    etree.SubElement(entry, "BILLEDQTY").text = tally_quantity(
        item.quantity, item.unit
    )

    if item.godown:
        # Only when a godown was asked for: an unnecessary batch block is one
        # more thing for Tally to reject silently.
        batch = etree.SubElement(entry, "BATCHALLOCATIONS.LIST")
        etree.SubElement(batch, "GODOWNNAME").text = item.godown
        etree.SubElement(batch, "AMOUNT").text = format(signed, "f")
        etree.SubElement(batch, "ACTUALQTY").text = tally_quantity(
            item.quantity, item.unit
        )
        etree.SubElement(batch, "BILLEDQTY").text = tally_quantity(
            item.quantity, item.unit
        )


def build_voucher_delete_element(
    remote_id: str, voucher_type: VoucherType, when: date
) -> etree._Element:
    """A ``<VOUCHER ACTION="Delete">`` addressed by our own REMOTEID.

    Tally needs the type and date alongside the identity; without them it
    answers "Cannot delete unnamed object: VOUCHER!" and changes nothing.
    """
    element = etree.Element(
        "VOUCHER",
        attrib={
            "VCHTYPE": voucher_type.value,
            "ACTION": "Delete",
            "REMOTEID": remote_id,
        },
    )
    etree.SubElement(element, "DATE").text = to_tally_date(when)
    etree.SubElement(element, "VOUCHERTYPENAME").text = voucher_type.value
    return element


def build_cost_category_element(
    category: CostCategory, action: str = "Create"
) -> etree._Element:
    """A cost category. Tally ships "Primary Cost Category"; this makes more."""
    element = etree.Element(
        "COSTCATEGORY", attrib={"NAME": category.name, "ACTION": action}
    )
    etree.SubElement(element, "NAME").text = category.name
    etree.SubElement(element, "ALLOCATEREVENUE").text = (
        "Yes" if category.allocate_revenue else "No"
    )
    etree.SubElement(element, "ALLOCATENONREVENUE").text = (
        "Yes" if category.allocate_non_revenue else "No"
    )
    return element


def build_cost_centre_element(
    centre: CostCentre, action: str = "Create"
) -> etree._Element:
    """A cost centre, inside a category that must already exist in the company."""
    element = etree.Element("COSTCENTRE", attrib={"NAME": centre.name, "ACTION": action})
    etree.SubElement(element, "NAME").text = centre.name
    etree.SubElement(element, "PARENT").text = centre.parent
    etree.SubElement(element, "CATEGORY").text = centre.category
    return element


def build_unit_element(unit: Unit, action: str = "Create") -> etree._Element:
    """A unit of measure. Tally calls the symbol NAME and the long form
    FORMALNAME.

    No ORIGINALNAME: on a Create it makes TallyPrime 1.1.7.1 answer
    "DUPLICATE ORIGINAL NAME" and write nothing. It is the *previous* name of a
    unit being renamed, so it only belongs on an Alter.
    """
    element = etree.Element("UNIT", attrib={"NAME": unit.name, "ACTION": action})
    etree.SubElement(element, "NAME").text = unit.name
    if unit.formal_name and unit.formal_name != unit.name:
        # A FORMALNAME equal to the symbol is also "DUPLICATE ORIGINAL NAME" to
        # TallyPrime 1.1.7.1 - the two names must differ or be absent.
        etree.SubElement(element, "FORMALNAME").text = unit.formal_name
    etree.SubElement(element, "DECIMALPLACES").text = str(unit.decimal_places)
    etree.SubElement(element, "ISSIMPLEUNIT").text = "Yes"
    return element


def build_stock_group_element(
    group: StockGroup, action: str = "Create"
) -> etree._Element:
    element = etree.Element(
        "STOCKGROUP", attrib={"NAME": group.name, "ACTION": action}
    )
    etree.SubElement(element, "NAME").text = group.name
    if group.parent:
        etree.SubElement(element, "PARENT").text = group.parent
    return element


def build_godown_element(godown: Godown, action: str = "Create") -> etree._Element:
    element = etree.Element("GODOWN", attrib={"NAME": godown.name, "ACTION": action})
    etree.SubElement(element, "NAME").text = godown.name
    if godown.parent:
        etree.SubElement(element, "PARENT").text = godown.parent
    return element


def build_stock_item_element(
    item: StockItem, action: str = "Create"
) -> etree._Element:
    """A stock item, with its opening stock if it has any."""
    element = etree.Element("STOCKITEM", attrib={"NAME": item.name, "ACTION": action})
    etree.SubElement(element, "NAME").text = item.name
    if item.parent:
        etree.SubElement(element, "PARENT").text = item.parent
    etree.SubElement(element, "BASEUNITS").text = item.base_units
    if item.hsn_code:
        etree.SubElement(element, "HSNCODE").text = item.hsn_code
    if item.gst_rate is not None:
        etree.SubElement(element, "GSTAPPLICABLE").text = "Applicable"
        etree.SubElement(element, "RATEOFVAT").text = format(item.gst_rate, "f")
    if item.opening_quantity:
        etree.SubElement(element, "OPENINGBALANCE").text = tally_quantity(
            item.opening_quantity, item.base_units
        )
        etree.SubElement(element, "OPENINGRATE").text = tally_rate(
            item.opening_rate, item.base_units
        )
        etree.SubElement(element, "OPENINGVALUE").text = format(
            item.opening_value, "f"
        )
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
