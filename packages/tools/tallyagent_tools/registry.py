"""The tool registry: one description of every tool, for the LLM and for MCP.

JSON Schemas are written by hand rather than derived from signatures. They are
the prompt the model reads, so the wording is load-bearing - "never invent a
ledger" belongs in a description, not only in a docstring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tallyagent_tools import ingest, masters, reconcile, reports, vouchers
from tallyagent_tools.base import ToolContext, ToolResult

ToolFn = Callable[..., Awaitable[ToolResult]]

_STRING = {"type": "string"}
_NUMBER = {"type": "string", "description": "Decimal as a string, e.g. \"11800.00\""}
_DATE = {"type": "string", "description": "ISO date, YYYY-MM-DD"}


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: ToolFn
    #: Write tools never execute directly over MCP - they return a ticket.
    mutating: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: tuple[Tool, ...] = (
    # --- reads --------------------------------------------------------------
    Tool(
        "trial_balance",
        "Closing balance of every ledger. Positive means a debit balance.",
        _object({"group": {**_STRING, "description": "Optional group filter."}}),
        reports.trial_balance,
    ),
    Tool(
        "outstanding_receivables",
        "What customers owe, per party and in ageing buckets.",
        _object({"as_on": _DATE, "party": _STRING}),
        reports.outstanding_receivables,
    ),
    Tool(
        "outstanding_payables",
        "What we owe suppliers, per party and in ageing buckets.",
        _object({"as_on": _DATE, "party": _STRING}),
        reports.outstanding_payables,
    ),
    Tool(
        "cash_position",
        "Balances of all bank and cash ledgers.",
        _object({}),
        reports.cash_position,
    ),
    Tool(
        "top_debtors",
        "Largest receivable balances, biggest first.",
        _object({"limit": {"type": "integer", "default": 5}}),
        reports.top_debtors,
    ),
    Tool(
        "day_book",
        "Every voucher line in a date range.",
        _object({"from_date": _DATE, "to_date": _DATE}),
        reports.day_book,
    ),
    Tool(
        "party_transactions",
        "The most recent transactions with one party.",
        _object(
            {"party_name": _STRING, "limit": {"type": "integer", "default": 10}},
            ["party_name"],
        ),
        reports.party_transactions,
    ),
    Tool(
        "gstr1_data",
        "Outward supplies for a period in GSTR-1 shape.",
        _object({"from_date": _DATE, "to_date": _DATE}),
        reports.gstr1_data,
    ),
    Tool(
        "gstr2_purchase_register",
        "Inward supplies for a period - the book side of a GSTR-2B reconciliation.",
        _object({"from_date": _DATE, "to_date": _DATE}),
        reports.gstr2_purchase_register,
    ),
    Tool(
        "find_voucher",
        "Find a posted voucher and its Tally id. Call this before alter_voucher: "
        "amending without the id makes Tally create a duplicate instead of "
        "refusing.",
        _object(
            {
                "reference": {**_STRING, "description": "Invoice number on the voucher"},
                "voucher_number": _STRING,
                "voucher_type": _STRING,
                "from_date": _DATE,
                "to_date": _DATE,
            }
        ),
        reports.find_voucher,
    ),
    Tool(
        "list_ledgers",
        "List ledger masters, optionally filtered to one group.",
        _object({"group": _STRING, "refresh": {"type": "boolean", "default": False}}),
        masters.list_ledgers,
    ),
    Tool(
        "resolve_ledger_alias",
        "Map a name someone typed to a real ledger. Returns suggestions rather "
        "than guessing. Always call this before using a ledger name you are "
        "not certain about; never invent a ledger.",
        _object({"name": _STRING}, ["name"]),
        masters.resolve_ledger_alias,
    ),
    Tool(
        "bank_reco",
        "Match bank statement rows against the bank ledger and propose vouchers "
        "for unmatched lines. Proposes only; posts nothing.",
        _object(
            {
                "statement_rows": {
                    "type": "array",
                    "items": _object(
                        {
                            "date": _DATE,
                            "narration": _STRING,
                            "amount": _NUMBER,
                            "reference": _STRING,
                        },
                        ["date", "amount"],
                    ),
                },
                "bank_ledger": _STRING,
                "from_date": _DATE,
                "to_date": _DATE,
            },
            ["statement_rows"],
        ),
        reconcile.bank_reco,
    ),
    Tool(
        "gstr2b_vs_purchase_register",
        "Compare a GSTR-2B JSON against the purchase register and classify every "
        "invoice as matched, missing in books, missing in 2B, or value mismatch.",
        _object(
            {
                "gstr2b_json": {"type": "object"},
                "from_date": _DATE,
                "to_date": _DATE,
            },
            ["gstr2b_json"],
        ),
        reconcile.gstr2b_vs_purchase_register,
    ),
    Tool(
        "bank_statement_to_rows",
        "Parse bank statement text or CSV into rows for bank_reco.",
        _object(
            {"content": _STRING, "fmt": {"enum": ["auto", "csv", "text"]}}, ["content"]
        ),
        ingest.bank_statement_to_rows,
    ),
    # --- writes -------------------------------------------------------------
    Tool(
        "create_sales_voucher",
        "Raise a sales invoice. The party ledger must already exist. The result "
        "is queued for human approval unless policy says otherwise.",
        _object(
            {
                "party_name": _STRING,
                "taxable_value": _NUMBER,
                "gst_rate": {**_NUMBER, "description": "One of 0, 0.1, 0.25, 3, 5, 12, 18, 28"},
                "voucher_date": _DATE,
                "reference": {**_STRING, "description": "Invoice number"},
                "narration": _STRING,
                "sales_ledger": _STRING,
                "place_of_supply": {**_STRING, "description": "Two-digit GST state code"},
            },
            ["party_name", "taxable_value"],
        ),
        vouchers.create_sales_voucher,
        mutating=True,
    ),
    Tool(
        "create_purchase_voucher",
        "Book a supplier bill. The party ledger must already exist.",
        _object(
            {
                "party_name": _STRING,
                "taxable_value": _NUMBER,
                "gst_rate": _NUMBER,
                "voucher_date": _DATE,
                "reference": _STRING,
                "narration": _STRING,
                "purchase_ledger": _STRING,
                "place_of_supply": _STRING,
            },
            ["party_name", "taxable_value"],
        ),
        vouchers.create_purchase_voucher,
        mutating=True,
    ),
    Tool(
        "create_payment",
        "Pay a supplier from a bank or cash ledger.",
        _object(
            {
                "party_name": _STRING,
                "amount": _NUMBER,
                "bank_ledger": _STRING,
                "voucher_date": _DATE,
                "reference": _STRING,
                "narration": _STRING,
            },
            ["party_name", "amount"],
        ),
        vouchers.create_payment,
        mutating=True,
    ),
    Tool(
        "create_receipt",
        "Record money received from a customer.",
        _object(
            {
                "party_name": _STRING,
                "amount": _NUMBER,
                "bank_ledger": _STRING,
                "voucher_date": _DATE,
                "reference": _STRING,
                "narration": _STRING,
            },
            ["party_name", "amount"],
        ),
        vouchers.create_receipt,
        mutating=True,
    ),
    Tool(
        "create_journal",
        "Free-form journal. Amounts are positive for debit, negative for credit, "
        "and must sum to zero.",
        _object(
            {
                "lines": {
                    "type": "array",
                    "items": _object(
                        {"ledger": _STRING, "amount": _NUMBER}, ["ledger", "amount"]
                    ),
                },
                "voucher_date": _DATE,
                "narration": _STRING,
                "reference": _STRING,
            },
            ["lines"],
        ),
        vouchers.create_journal,
        mutating=True,
    ),
    Tool(
        "alter_voucher",
        "Amend an existing voucher. The master_id must come from find_voucher: "
        "Tally does not reject an unmatched id, it silently creates a duplicate.",
        _object(
            {
                "master_id": _STRING,
                "lines": {
                    "type": "array",
                    "items": _object(
                        {"ledger": _STRING, "amount": _NUMBER}, ["ledger", "amount"]
                    ),
                },
                "voucher_type": _STRING,
                "voucher_date": _DATE,
                "narration": _STRING,
            },
            ["master_id", "lines"],
        ),
        vouchers.alter_voucher,
        mutating=True,
    ),
    Tool(
        "create_ledger",
        "Create a ledger master under an existing group. Only do this when the "
        "user has explicitly asked for a new ledger.",
        _object(
            {
                "name": _STRING,
                "group": _STRING,
                "opening_balance": _NUMBER,
                "gstin": _STRING,
                "state": _STRING,
            },
            ["name", "group"],
        ),
        masters.create_ledger,
        mutating=True,
    ),
    Tool(
        "create_party",
        "Create a customer or supplier ledger.",
        _object(
            {
                "name": _STRING,
                "is_customer": {"type": "boolean", "default": True},
                "gstin": _STRING,
                "state_code": _STRING,
                "credit_period_days": {"type": "integer"},
            },
            ["name"],
        ),
        masters.create_party,
        mutating=True,
    ),
    Tool(
        "invoice_image_to_draft",
        "Turn an extracted invoice (JSON from a vision model) into a validated, "
        "queued voucher draft.",
        _object(
            {
                "extraction": {
                    "type": ["string", "object"],
                    "description": "The vision model's JSON reply",
                },
                "as_purchase": {"type": "boolean", "default": True},
            },
            ["extraction"],
        ),
        ingest.invoice_image_to_draft,
        mutating=True,
    ),
)

BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def get(name: str) -> Tool:
    if name not in BY_NAME:
        raise KeyError(
            f"no tool named {name!r}. Available: {', '.join(sorted(BY_NAME))}"
        )
    return BY_NAME[name]


def schemas(mutating: bool | None = None) -> list[dict[str, Any]]:
    """Tool schemas, optionally filtered to reads (False) or writes (True)."""
    return [
        tool.schema()
        for tool in TOOLS
        if mutating is None or tool.mutating is mutating
    ]


def openai_schemas() -> list[dict[str, Any]]:
    return [tool.openai_schema() for tool in TOOLS]


async def call(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> ToolResult:
    """Invoke a tool by name with the model's arguments.

    Dates arrive as ISO strings from JSON and are coerced here so every tool can
    declare a ``date`` parameter honestly.
    """
    tool = get(name)
    coerced = dict(arguments)
    for key in ("voucher_date", "from_date", "to_date", "as_on"):
        value = coerced.get(key)
        if isinstance(value, str) and value:
            from datetime import date as _date

            coerced[key] = _date.fromisoformat(value)
        elif value in ("", None) and key in coerced:
            coerced.pop(key)
    return await tool.fn(ctx, **coerced)
