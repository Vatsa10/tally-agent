# Stage 15: amend/delete, inventory, cost centres, multi-currency

Design for the four subsystems approved on 2026-09-12. Payroll is explicitly out
of scope: it is a separate domain (attendance, pay heads, salary structures,
PF/ESI/PT/TDS) and belongs in its own spec.

Target: TallyPrime 1.1.7.1 Educational, the instance everything else in
`docs/LIVE_SETUP.md` was verified against.

---

## 15.1 Amend and delete vouchers

### The problem, restated correctly

"Impossible over XML" is true and unfixable. Measured on 1.1.7.1: an import with
`ACTION="Alter"` carrying `MASTERID`, `REMOTEID`, `VCHKEY` or `GUID` creates a
*new* voucher rather than amending the named one, and `ACTION="Delete"` and
`ACTION="Cancel"` do nothing. No payload shape changes this.

But amending and deleting are not blocked — they are on the **wrong tier**. Both
work through the UI (Day Book → select → `Alt+D` to delete, `Enter` to alter),
which is exactly what Tier 3 exists for.

### Design

- `alter_voucher` and a new `delete_voucher` keep their current tool signatures.
- Each first **attempts XML**, because a later Tally may support it and the
  attempt is cheap. The result is verified by re-reading the voucher: if the
  count went up, or the target is unchanged, the attempt failed.
- On failure, fall through to Tier 3 with a task description naming the voucher
  by number, date and amount so the fallback can find it in the Day Book.
- Every mutating keystroke goes through the existing approval hook, with the
  braille screenshot preview.
- **Verification is mandatory and separate from the attempt.** The tool reports
  success only after re-reading Tally and confirming the voucher changed or is
  gone. A UI path that "completed" without the books changing is a failure.
- `voucher_deleted` / `voucher_altered` audit events record which tier was used.

### Why not skip the XML attempt

It costs one request, it documents the capability per Tally version in the audit
log, and it is the only thing that will tell us when a future version fixes it.

---

## 15.2 Inventory

The substantial piece. A stock-tracking trader needs a sales voucher to move
quantity, not just value.

### New masters

| Master | Tally type | Fields we set |
|---|---|---|
| Unit | `Unit` | name (symbol), formal name, decimal places |
| Stock group | `StockGroup` | name, parent |
| Stock item | `StockItem` | name, parent group, base units, HSN, GST rate, opening qty/rate |
| Godown | `Godown` | name, parent |

All created through the existing batched-masters approval path
(`seed_chart_of_accounts`'s mechanism), not a new one.

### Voucher model change

`Voucher` gains `inventory: list[InventoryLine]` alongside `lines`.

```python
class InventoryLine(BaseModel):
    stock_item: str
    quantity: Decimal          # positive = inward, negative = outward
    rate: Decimal              # per unit, in base units
    unit: str = ""             # blank = the item's base unit
    godown: str = ""           # blank = the default godown
    amount: Decimal | None = None   # derived: quantity * rate, unless overridden
```

`amount` is derived rather than required, because quantity × rate is the invariant
and letting a caller supply all three invites them to disagree.

### XML

`ALLINVENTORYENTRIES.LIST` per line, with `BATCHALLOCATIONS.LIST` for the godown,
mirroring the vendored `tally-mcp` shape (which is known to work) but built with
lxml like everything else. `RATE` carries the unit suffix Tally expects
(`100/Nos`), which is a formatting quirk worth isolating in `quirks.py`.

### Validation (new rules)

- `stock_items_exist` — every stock item resolves to a master, with fuzzy
  suggestions, never silent creation. Same shape as `ledgers_exist`.
- `inventory_matches_value` — the sum of inventory line amounts equals the
  taxable value on the voucher. This is the rule that catches the classic error:
  a sales voucher whose stock moved ₹10,000 of goods but whose ledgers booked
  ₹1,00,000 of revenue.
- `stock_not_negative` — an outward movement may not take an item below zero on
  hand, unless the company permits negative stock. Warning, not error, because
  Tally itself allows it and some traders rely on it.

### Reports

- `stock_summary` — quantity and value on hand per item, derived from opening
  plus movements (the same derivation the trial balance uses, for the same
  reason: report exports return nothing).
- `stock_ledger(item)` — movements for one item.

### Tools

`create_sales_voucher` / `create_purchase_voucher` gain an optional `items`
argument. When present, the voucher carries inventory and the taxable value is
computed from the items rather than passed in. When absent, behaviour is exactly
as today — this must not break the accounting-only path.

---

## 15.3 Cost centres

Half-built already: `VoucherLine.cost_centre` exists and the XML emits
`CATEGORYALLOCATIONS.LIST` / `COSTCENTREALLOCATIONS.LIST`. Never tested live, no
masters, no validation.

- Masters: `CostCategory`, `CostCentre` (name, parent, category).
- Validation: `cost_centres_exist`; and if a company has cost centres at all,
  warn when a P&L-affecting line has none — that is the error that makes cost
  reporting useless six months later.
- Report: `cost_centre_summary` — net per cost centre, derived from postings.

---

## 15.4 Multi-currency

- Master: `Currency` (symbol, formal name, decimal places, is base).
- `VoucherLine` gains `currency` and `rate_of_exchange`; base-currency amount
  stays the one that balances, so the debit==credit rule is untouched.
- Validation: `currency_exists`, and `exchange_rate_present` when a line's
  currency is not the base one.
- Reports stay in base currency. Foreign-currency reporting is a separate ask.

---

## What does not change

- Every write still goes through `base.submit`: scope → validate → queue →
  approve. No new path to the backend.
- Live-mode write scope, EDU date rule, idempotency, audit chain: untouched.
- The fake server reproduces every new shape, including the refusals, so CI
  exercises what production meets.

## Order and risk

1. **15.1 amend/delete** — small, unblocks a real gap, reuses Tier 3.
2. **15.2 inventory** — largest; the reason this stage exists.
3. **15.3 cost centres** — small once 15.2's master pattern is in place.
4. **15.4 multi-currency** — smallest surface, least demand; last.

Main risk is Tally crashing on unfamiliar XML (it has, repeatedly). Mitigation:
health-check after every new payload shape, and `scripts/recover_tally.py` for
when it does.
