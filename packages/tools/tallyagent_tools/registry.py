"""The tool registry: one description of every tool, for the LLM and for MCP.

JSON Schemas are written by hand rather than derived from signatures. They are
the prompt the model reads, so the wording is load-bearing - "never invent a
ledger" belongs in a description, not only in a docstring.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tallyagent_tools import (
    cost_centres,
    currencies,
    ingest,
    masters,
    reconcile,
    reports,
    stock,
    vouchers,
)
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
                "items": {
                    "type": "array",
                    "description": (
                        "Stock moved by this voucher. Give a positive quantity; "
                        "the direction comes from the voucher type. When present "
                        "the taxable value is computed from the goods."
                    ),
                    "items": _object(
                        {
                            "stock_item": _STRING,
                            "quantity": _NUMBER,
                            "rate": _NUMBER,
                            "unit": _STRING,
                            "godown": _STRING,
                        },
                        ["stock_item", "quantity", "rate"],
                    ),
                },
            },
            ["party_name"],
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
                "items": {
                    "type": "array",
                    "description": (
                        "Stock moved by this voucher. Give a positive quantity; "
                        "the direction comes from the voucher type. When present "
                        "the taxable value is computed from the goods."
                    ),
                    "items": _object(
                        {
                            "stock_item": _STRING,
                            "quantity": _NUMBER,
                            "rate": _NUMBER,
                            "unit": _STRING,
                            "godown": _STRING,
                        },
                        ["stock_item", "quantity", "rate"],
                    ),
                },
            },
            ["party_name"],
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
                        {"ledger": _STRING, "amount": _NUMBER, "cost_centre": _STRING},
                        ["ledger", "amount"],
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
        "Amend a voucher tallyagent posted, identified by the REMOTEID from "
        "find_voucher. Replaces the voucher's lines entirely, so send every "
        "line, not only the changed one.",
        _object(
            {
                "remote_id": _STRING,
                "lines": {
                    "type": "array",
                    "items": _object(
                        {
                            "ledger": _STRING,
                            "amount": _NUMBER,
                            "cost_centre": _STRING,
                            "bill_reference": _STRING,
                        },
                        ["ledger", "amount"],
                    ),
                },
                "voucher_type": _STRING,
                "voucher_date": _DATE,
                "narration": _STRING,
                "reference": _STRING,
                "party_name": _STRING,
            },
            ["remote_id", "lines"],
        ),
        vouchers.alter_voucher,
        mutating=True,
    ),
    Tool(
        "delete_voucher",
        "Delete a voucher tallyagent posted, identified by the REMOTEID from "
        "find_voucher. Queued for approval showing what will disappear.",
        _object(
            {
                "remote_id": _STRING,
                "voucher_type": _STRING,
                "voucher_date": _DATE,
                "reason": {**_STRING, "description": "Why it is being deleted"},
            },
            ["remote_id"],
        ),
        vouchers.delete_voucher,
        mutating=True,
    ),
    Tool(
        "list_stock_items",
        "The stock item masters, with opening quantities.",
        _object({"refresh": {"type": "boolean", "default": False}}),
        stock.list_stock_items,
    ),
    Tool(
        "stock_summary",
        "Quantity and value of every stock item on hand, at weighted average "
        "cost. Flags any item that has gone negative.",
        _object({"as_on": _DATE}),
        stock.stock_summary,
    ),
    Tool(
        "stock_ledger",
        "Every movement of one stock item, with a running balance.",
        _object(
            {"stock_item": _STRING, "from_date": _DATE, "to_date": _DATE},
            ["stock_item"],
        ),
        stock.stock_ledger,
    ),
    Tool(
        "create_stock_masters",
        "Create stock items, units, stock groups and godowns as one batch. "
        "Stock items must exist before a voucher can move them; nothing is "
        "created implicitly.",
        _object(
            {
                "items": {
                    "type": "array",
                    "items": _object(
                        {
                            "name": _STRING,
                            "group": _STRING,
                            "unit": _STRING,
                            "hsn": _STRING,
                            "gst_rate": _NUMBER,
                            "opening_quantity": _NUMBER,
                            "opening_rate": _NUMBER,
                        },
                        ["name"],
                    ),
                },
                "units": {"type": "array", "items": _STRING},
                "groups": {"type": "array", "items": _STRING},
                "godowns": {"type": "array", "items": _STRING},
            }
        ),
        stock.create_stock_masters,
        mutating=True,
    ),
    Tool(
        "list_currencies",
        "The currencies this company has masters for.",
        _object({}),
        currencies.list_currencies,
    ),
    Tool(
        "create_currencies",
        "Create currency masters. Refused outright on a TallyPrime that cannot "
        "survive the import.",
        _object(
            {
                "currencies": {
                    "type": "array",
                    "items": _object(
                        {"symbol": _STRING, "name": _STRING, "decimal_places": _NUMBER},
                        ["symbol"],
                    ),
                }
            }
        ),
        currencies.create_currencies,
        mutating=True,
    ),
    Tool(
        "list_cost_centres",
        "The cost centres this company tracks, with their categories.",
        _object({}),
        cost_centres.list_cost_centres,
    ),
    Tool(
        "cost_centre_summary",
        "Net amount posted to each cost centre.",
        _object({}),
        cost_centres.cost_centre_summary,
    ),
    Tool(
        "create_cost_centres",
        "Create cost categories and cost centres as one batch. A voucher can "
        "only be allocated to a cost centre that already exists.",
        _object(
            {
                "centres": {
                    "type": "array",
                    "items": _object(
                        {"name": _STRING, "category": _STRING, "parent": _STRING},
                        ["name"],
                    ),
                },
                "categories": {"type": "array", "items": _STRING},
            }
        ),
        cost_centres.create_cost_centres,
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

if len(BY_NAME) != len(TOOLS):
    # A duplicate name silently shadows one definition and the model gets a
    # schema that does not match the function it reaches.
    _seen: set[str] = set()
    _dupes = sorted({t.name for t in TOOLS if t.name in _seen or _seen.add(t.name)})
    raise RuntimeError(f"duplicate tool name(s) in the registry: {_dupes}")


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
