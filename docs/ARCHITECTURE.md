# Architecture

## The three tiers

```
                         ┌──────────────────── THE MACHINE ────────────────────┐
                         │  (a CA firm's Windows PC, or a container on the LAN) │
                         │                                                      │
  WhatsApp ──┐           │   ┌──────────────┐                                   │
  Web UI ────┼──────────►│   │  agent loop  │  max 12 steps, traced per step    │
  MCP client ┘           │   └──────┬───────┘                                   │
                         │          │ tool call                                 │
                         │          ▼                                           │
                         │   ┌──────────────┐                                   │
                         │   │ typed tools  │  registry.py                      │
                         │   └──────┬───────┘                                   │
                         │          │                                           │
                         │          ▼                                           │
                         │   ┌──────────────────────┐                           │
                         │   │ deterministic        │  ledgers exist, Dr==Cr,   │
                         │   │ validation           │  GST rate + split, GSTIN, │
                         │   └──────┬───────────────┘  period open, duplicates  │
                         │          │ passes                                    │
                         │          ▼                                           │
                         │   ┌──────────────────────┐                           │
                         │   │ approval queue       │◄── a human approves ──────┼──
                         │   │ (+ hash-chained      │                           │
                         │   │    audit log)        │                           │
                         │   └──────┬───────────────┘                           │
                         │          │ approved                                  │
                         │          ▼                                           │
   ══════ TIER 1 ════════╪══ ┌──────────────┐ ═══════════ the only write path ══╪══
    API execution        │   │ Tally XML    │ ──── XML over HTTP ───► TallyPrime │
                         │   │ adapter      │      localhost:9000                │
                         │   └──────────────┘                                   │
                         │                                                      │
   ══════ TIER 2 ════════╪══ ┌──────────────┐                                   │
    perception only      │   │ screen.py    │ ──► crops to the Tally window,     │
    (off by default)     │   └──────────────┘     classifies, stores ui_context  │
                         │                        NEVER writes                   │
                         │                                                      │
   ══════ TIER 3 ════════╪══ ┌──────────────┐                                   │
    gated fallback       │   │computer_use  │ ──► bounded, recorded, every       │
    (off by default)     │   └──────────────┘     mutating step needs approval   │
                         │                                                      │
                         │   ┌──────────────────────────────────────────────┐   │
                         │   │ SQLite: approvals, audit, idempotency,        │  │
                         │   │ memory, egress log                            │  │
                         │   └──────────────────────────────────────────────┘   │
                         └──────────────────────┬───────────────────────────────┘
                                                │
      ════════════════ TRUST BOUNDARY ══════════╪══════════════════════════════════
                                                │
                                                ▼
                                      ┌──────────────────┐
                                      │ model provider   │  api.deepseek.com
                                      │ (DeepSeek et al) │  by default
                                      └──────────────────┘
                Crosses the boundary: the system prompt (company NAME, FY, state
                code, GSTIN), the user's message, tool *results* the agent chose
                to send (truncated to 4000 chars), and invoice/screen images.
                Every crossing is measured and written to the egress log.

                Never crosses: the chart of accounts in bulk, the approval
                queue, the audit log, the idempotency keys, credentials, or any
                screenshot wider than the Tally window.
```

## Why the tiers are ordered this way

**Tier 1 is not a preference, it is the rule.** Every read and every write goes
through TallyPrime's XML-over-HTTP interface. It is deterministic, it returns
structured errors, and it can be replayed and tested. Screen automation cannot
do any of those things, so it never touches data.

**Tier 2 exists because context is cheap and mistakes are not.** Knowing that
the user is staring at a Trial Balance for Q1 makes "what does this show?"
answerable. It is read-only by construction: `perception/screen.py` has no
write path at all, and it captures only the Tally window's rectangle.

**Tier 3 exists to make a gap visible, not to fill it.** Every fallback run
emits a `fallback_used` event naming the Tier 1 tool that should have existed.
If the event recurs, the answer is to build the tool.

## Data flow: an invoice photo to a posted voucher

1. **WhatsApp webhook** verifies the HMAC signature, then checks the sender
   against the allowlist. An unknown number is refused before its text is read.
2. **Media download** fetches the image (≤8 MB) through the Graph API.
3. **Agent loop** sends the image to the model wrapped in a prompt that names
   the document as data, never as instructions.
4. **`ingest.parse_invoice_json`** coerces the reply into typed fields, checks
   the GSTIN checksum, derives the GST rate rather than trusting the printed
   one, and records warnings.
5. **`resolve_ledger_alias`** must find the vendor as an existing master.
   A near match is offered as a suggestion; it is never applied.
6. **`create_purchase_voucher`** builds a balanced draft, choosing IGST or
   CGST+SGST from the party's state against the company's.
7. **Validation** runs every rule. A failure blocks enqueue unless a human
   overrides with a reason, which is logged.
8. **Approval queue** stores the draft with an idempotency key derived from the
   voucher's content, and renders a diff: ledger impact, totals, the validation
   report, and the exact XML.
9. **A person approves.** Only then does the executor call the backend.
10. **The backend's idempotency gate** records the key as in-flight before the
    envelope leaves. The same invoice photographed twice collides on
    fingerprint at step 7 (duplicate detection) and on key at step 10.

## Package layout and dependency direction

```
core        ← domain and validation. No I/O. Depends on nothing.
backends    ← the AccountingBackend protocol. Depends on core.
tally       ← the TallyPrime adapter + fake server. Depends on core, backends.
tools       ← typed tools. Depends on core, backends, tally.
llm         ← providers and the router. Depends on core.
approvals   ← queue, audit, SQLite. Depends on core, backends, tools.
agent       ← loop, memory, context, tiers. Depends on tools, llm, approvals.
channels    ← web, WhatsApp, shared Services. Depends on agent, approvals, tools.
mcp_server  ← MCP over the registry. Depends on tools.
daemon      ← config, wiring, app, scheduler, CLI. Depends on everything.
```

Arrows only point down that list. `core` is `mypy --strict` clean and has no
imports from anything in this repo.

## Where the invariants live

| Invariant | Enforced in | Test |
|---|---|---|
| Writes go only through validate-then-queue | `tools/base.py::submit` | `test_tools.py`, `test_mcp_server.py` |
| No mutation without human approval | `approvals/queue.py::approve` | `test_approvals.py` |
| A replayed write is a no-op | `tally/backend.py::_import_once` | `test_tally_adapter.py`, `test_end_to_end.py` |
| Debits equal credits to the paisa | `core/validation/rules.py::balanced` | `test_core_validation.py` |
| Tier 2 never writes, Tier 3 needs approval | `agent/tiers.py`, `fallback/computer_use.py` | `test_tiers_perception_fallback.py` |
| What left the machine is recorded | `llm/router.py`, `daemon/wiring.py` | `test_llm.py`, `test_daemon.py` |
| The audit log is tamper-evident | `approvals/audit.py` | `test_approvals.py` |
