from xml.sax.saxutils import escape, quoteattr

from tally_mcp.models import LedgerCreate, VoucherCreate, StockItemCreate, GroupCreate


def build_ledger_xml(ledger: LedgerCreate, action: str = "Create") -> str:
    parts = [f'<LEDGER NAME={quoteattr(ledger.name)} ACTION="{action}">']
    parts.append(f"<NAME>{escape(ledger.name)}</NAME>")
    parts.append(f"<PARENT>{escape(ledger.parent)}</PARENT>")
    if ledger.opening_balance:
        parts.append(f"<OPENINGBALANCE>{ledger.opening_balance}</OPENINGBALANCE>")
    if ledger.address:
        parts.append(f"<ADDRESS.LIST><ADDRESS>{escape(ledger.address)}</ADDRESS></ADDRESS.LIST>")
    if ledger.gst_number:
        parts.append(f"<PARTYGSTIN>{escape(ledger.gst_number)}</PARTYGSTIN>")
    parts.append("</LEDGER>")
    return "\n".join(parts)


def build_voucher_xml(voucher: VoucherCreate) -> str:
    # NOTE: OBJVIEW attribute causes Tally crash (c0000005 memory access violation).
    # Official Tally docs (Case Study 1) don't use OBJVIEW at all.
    # Tally defaults to Accounting Voucher View when no view is specified.
    parts = [f'<VOUCHER VCHTYPE={quoteattr(voucher.voucher_type)} ACTION="Create">']
    parts.append(f"<DATE>{escape(voucher.date)}</DATE>")
    parts.append(f"<VOUCHERTYPENAME>{escape(voucher.voucher_type)}</VOUCHERTYPENAME>")
    if voucher.party_name:
        parts.append(f"<PARTYLEDGERNAME>{escape(voucher.party_name)}</PARTYLEDGERNAME>")
    if voucher.narration:
        parts.append(f"<NARRATION>{escape(voucher.narration)}</NARRATION>")
    if voucher.reference:
        parts.append(f"<REFERENCE>{escape(voucher.reference)}</REFERENCE>")

    for entry in voucher.entries:
        xml_amount = -entry.amount
        deemed = "Yes" if xml_amount < 0 else "No"
        parts.append("<ALLLEDGERENTRIES.LIST>")
        parts.append(f"<LEDGERNAME>{escape(entry.ledger_name)}</LEDGERNAME>")
        parts.append(f"<ISDEEMEDPOSITIVE>{deemed}</ISDEEMEDPOSITIVE>")
        parts.append(f"<AMOUNT>{xml_amount}</AMOUNT>")
        if entry.cost_centre:
            parts.append("<CATEGORYALLOCATIONS.LIST>")
            parts.append("<CATEGORY>Primary Cost Category</CATEGORY>")
            parts.append("<COSTCENTREALLOCATIONS.LIST>")
            parts.append(f"<NAME>{escape(entry.cost_centre)}</NAME>")
            parts.append(f"<AMOUNT>{xml_amount}</AMOUNT>")
            parts.append("</COSTCENTREALLOCATIONS.LIST>")
            parts.append("</CATEGORYALLOCATIONS.LIST>")
        parts.append("</ALLLEDGERENTRIES.LIST>")

    for inv in voucher.inventory_entries:
        parts.append("<ALLINVENTORYENTRIES.LIST>")
        parts.append(f"<STOCKITEMNAME>{escape(inv.stock_item_name)}</STOCKITEMNAME>")
        parts.append(f"<RATE>{inv.rate}/{escape(inv.stock_item_name)}</RATE>")
        parts.append(f"<AMOUNT>{inv.amount}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{inv.quantity}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{inv.quantity}</BILLEDQTY>")
        parts.append("<BATCHALLOCATIONS.LIST>")
        parts.append(f"<GODOWNNAME>{escape(inv.godown)}</GODOWNNAME>")
        parts.append(f"<AMOUNT>{inv.amount}</AMOUNT>")
        parts.append(f"<ACTUALQTY>{inv.quantity}</ACTUALQTY>")
        parts.append(f"<BILLEDQTY>{inv.quantity}</BILLEDQTY>")
        parts.append("</BATCHALLOCATIONS.LIST>")
        parts.append("</ALLINVENTORYENTRIES.LIST>")

    parts.append("</VOUCHER>")
    return "\n".join(parts)


def build_stock_item_xml(item: StockItemCreate, action: str = "Create") -> str:
    parts = [f'<STOCKITEM NAME={quoteattr(item.name)} ACTION="{action}">']
    parts.append(f"<NAME>{escape(item.name)}</NAME>")
    if item.parent:
        parts.append(f"<PARENT>{escape(item.parent)}</PARENT>")
    parts.append(f"<BASEUNITS>{escape(item.unit)}</BASEUNITS>")
    if item.hsn_code:
        parts.append(f"<HSNCODE>{escape(item.hsn_code)}</HSNCODE>")
    if item.gst_rate:
        parts.append(f"<GSTRATE>{item.gst_rate}</GSTRATE>")
    parts.append("</STOCKITEM>")
    return "\n".join(parts)


def build_group_xml(group: GroupCreate, action: str = "Create") -> str:
    return (
        f'<GROUP NAME={quoteattr(group.name)} ACTION="{action}">'
        f"<NAME>{escape(group.name)}</NAME>"
        f"<PARENT>{escape(group.parent)}</PARENT>"
        f"</GROUP>"
    )
