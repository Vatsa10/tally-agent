"""Build human-readable confirmation summaries from Tally import XML.

Called after the XML request is framed but before it is sent, so the user
can review exactly what will happen and in which company.
"""

from lxml import etree


def summarize_import_xml(xml_bytes: bytes) -> str:
    """Parse a framed Tally import XML envelope and return a plain-text
    summary of what will be created/altered/cancelled and in which company."""
    root = etree.fromstring(xml_bytes)

    # Extract target company
    company = (
        root.findtext(".//REQUESTDESC/STATICVARIABLES/SVCURRENTCOMPANY") or "UNKNOWN"
    )

    # Extract all action elements from TALLYMESSAGE
    actions: list[str] = []
    for msg in root.iter("TALLYMESSAGE"):
        for child in msg:
            actions.append(_summarize_element(child))

    lines = [f"Company: {company}", ""]
    if actions:
        lines.append("Actions:")
        for i, action in enumerate(actions, 1):
            lines.append(f"  {i}. {action}")
    else:
        lines.append("No actions detected in request.")

    return "\n".join(lines)


def _summarize_element(elem: etree._Element) -> str:
    """Summarize a single LEDGER, VOUCHER, GROUP, or STOCKITEM element."""
    tag = elem.tag
    action = elem.get("ACTION", "Create")
    name = elem.get("NAME", "") or elem.findtext("NAME", "")

    if tag == "VOUCHER":
        return _summarize_voucher(elem, action)
    elif tag == "LEDGER":
        return _summarize_ledger(elem, action, name)
    elif tag == "GROUP":
        return _summarize_group(elem, action, name)
    elif tag == "STOCKITEM":
        return _summarize_stock_item(elem, action, name)
    else:
        return f"{action} {tag}: {name}" if name else f"{action} {tag}"


def _summarize_voucher(elem: etree._Element, action: str) -> str:
    vch_type = elem.findtext("VOUCHERTYPENAME", elem.get("VCHTYPE", "?"))
    date_raw = elem.findtext("DATE", "")
    date_str = _format_date(date_raw)
    party = elem.findtext("PARTYLEDGERNAME", "")
    narration = elem.findtext("NARRATION", "")
    reference = elem.findtext("REFERENCE", "")
    vch_number = elem.findtext("VOUCHERNUMBER", "")

    if action == "Cancel":
        return f"CANCEL {vch_type} voucher #{vch_number} dated {date_str}"

    # Collect ledger entries
    debits: list[str] = []
    credits: list[str] = []
    for entry in elem.iter("ALLLEDGERENTRIES.LIST"):
        ledger = entry.findtext("LEDGERNAME", "?")
        amount_str = entry.findtext("AMOUNT", "0")
        try:
            amount = float(amount_str)
        except ValueError:
            amount = 0.0
        if amount < 0:
            debits.append(f"{ledger} Dr {abs(amount):,.2f}")
        else:
            credits.append(f"{ledger} Cr {abs(amount):,.2f}")

    # Inventory entries
    inventory: list[str] = []
    for inv in elem.iter("ALLINVENTORYENTRIES.LIST"):
        item = inv.findtext("STOCKITEMNAME", "?")
        qty = inv.findtext("ACTUALQTY", "")
        rate = inv.findtext("RATE", "")
        inv_amt = inv.findtext("AMOUNT", "")
        inventory.append(f"{item} x{qty} @{rate} = {inv_amt}")

    parts = [f"{action} {vch_type} voucher on {date_str}"]
    if party:
        parts[0] += f" | Party: {party}"
    if reference:
        parts[0] += f" | Ref: {reference}"
    if debits:
        parts.append(f"    Debit:  {' | '.join(debits)}")
    if credits:
        parts.append(f"    Credit: {' | '.join(credits)}")
    if inventory:
        parts.append(f"    Items:  {' | '.join(inventory)}")
    if narration:
        parts.append(f"    Narration: {narration}")
    return "\n".join(parts)


def _summarize_ledger(elem: etree._Element, action: str, name: str) -> str:
    parent = elem.findtext("PARENT", "")
    opening = elem.findtext("OPENINGBALANCE", "")
    gst = elem.findtext("PARTYGSTIN", "")
    parts = [f"{action} Ledger: {name}"]
    if parent:
        parts.append(f"under {parent}")
    if opening and opening != "0":
        parts.append(f"opening bal {opening}")
    if gst:
        parts.append(f"GSTIN {gst}")
    return " | ".join(parts)


def _summarize_group(elem: etree._Element, action: str, name: str) -> str:
    parent = elem.findtext("PARENT", "")
    return f"{action} Group: {name} under {parent}" if parent else f"{action} Group: {name}"


def _summarize_stock_item(elem: etree._Element, action: str, name: str) -> str:
    unit = elem.findtext("BASEUNITS", "")
    hsn = elem.findtext("HSNCODE", "")
    gst = elem.findtext("GSTRATE", "")
    parts = [f"{action} Stock Item: {name}"]
    if unit:
        parts.append(f"unit {unit}")
    if hsn:
        parts.append(f"HSN {hsn}")
    if gst and gst != "0":
        parts.append(f"GST {gst}%")
    return " | ".join(parts)


def _format_date(raw: str) -> str:
    """Convert YYYYMMDD to DD-MMM-YYYY for readability."""
    if len(raw) == 8:
        try:
            from datetime import datetime

            dt = datetime.strptime(raw, "%Y%m%d")
            return dt.strftime("%d-%b-%Y")
        except ValueError:
            pass
    return raw or "?"
