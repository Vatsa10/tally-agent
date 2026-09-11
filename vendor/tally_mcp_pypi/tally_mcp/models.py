from pydantic import BaseModel, Field


class DateRange(BaseModel):
    from_date: str = Field("", description="Start date in YYYYMMDD format, e.g. 20250401")
    to_date: str = Field("", description="End date in YYYYMMDD format, e.g. 20260331")


class LedgerCreate(BaseModel):
    name: str = Field(description="Ledger name")
    parent: str = Field(description="Parent group name, e.g. 'Sundry Debtors', 'Sales Accounts'")
    opening_balance: float = Field(0.0, description="Opening balance amount")
    address: str = Field("", description="Address")
    gst_number: str = Field("", description="GST registration number")


class VoucherEntry(BaseModel):
    ledger_name: str = Field(description="Ledger name for this entry")
    amount: float = Field(description="Amount (positive = debit, negative = credit)")
    cost_centre: str = Field("", description="Cost centre name if applicable")


class InventoryEntry(BaseModel):
    stock_item_name: str = Field(description="Stock item name")
    quantity: float = Field(description="Quantity")
    rate: float = Field(description="Rate per unit")
    amount: float = Field(description="Total amount")
    godown: str = Field("Main Location", description="Godown name")


class VoucherCreate(BaseModel):
    voucher_type: str = Field(
        description="Voucher type: Sales, Purchase, Payment, Receipt, Journal, Contra, 'Credit Note', 'Debit Note'"
    )
    date: str = Field(description="Voucher date in YYYYMMDD format")
    party_name: str = Field("", description="Party ledger name (for Sales/Purchase)")
    narration: str = Field("", description="Narration/description")
    reference: str = Field("", description="Reference number")
    is_invoice: bool = Field(False, description="True for invoice mode, False for voucher mode")
    entries: list[VoucherEntry] = Field(description="Ledger entries (Dr/Cr). Must balance to zero.")
    inventory_entries: list[InventoryEntry] = Field(
        default_factory=list, description="Inventory entries (for Sales/Purchase with stock items)"
    )


class StockItemCreate(BaseModel):
    name: str = Field(description="Stock item name")
    parent: str = Field("", description="Stock group, e.g. 'Primary'")
    unit: str = Field("Nos", description="Unit of measure")
    opening_balance: float = Field(0.0, description="Opening quantity")
    opening_rate: float = Field(0.0, description="Opening rate per unit")
    opening_value: float = Field(0.0, description="Opening value")
    gst_rate: float = Field(0.0, description="GST rate percentage")
    hsn_code: str = Field("", description="HSN/SAC code")


class GroupCreate(BaseModel):
    name: str = Field(description="Group name")
    parent: str = Field(description="Parent group name")
