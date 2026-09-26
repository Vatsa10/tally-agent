# Tally Agent — build specification (end-to-end)

The full specification this monorepo was built against. Kept verbatim as the
record of intent; `docs/DECISIONS.md` records where implementation diverged.

---

`tallyagent` is a production-grade agentic bookkeeping system that operates TallyPrime on behalf of CA firms and SMB owners. Implement the entire monorepo end to end, with tests, in one pass. Do not stop to ask questions; make reasonable decisions, record them in `docs/DECISIONS.md`, and continue. Do not stub anything you can implement.

## Non-negotiable design principles

1. API-first execution. All reads and writes to Tally go through TallyPrime's XML-over-HTTP interface (default `http://localhost:9000`) via typed tools. Never use screen automation to mutate Tally.
2. Screen-awareness is for perception only (Tier 2) and gated fallback (Tier 3). Never for routine execution.
3. Every mutating action passes deterministic validation against Tally masters, then lands in an approval queue with a diff preview, unless the action type has been explicitly promoted to auto-approve in policy config.
4. Idempotency on every write: client-generated idempotency key per voucher, persisted; replays are no-ops.
5. Provider-agnostic model layer. Default model `deepseek-flash` via `https://api.deepseek.com` (OpenAI-compatible). Must be swappable to Anthropic/OpenAI/local via config only.
6. Tally data stays local. Only the minimum required context is sent to the model. Log exactly what left the machine per request.
7. Accounting-backend abstraction so Tally, Zoho Books, Busy can be added later; implement Tally fully, leave the interface plus a Zoho Books adapter skeleton that fails loudly.

## Reuse existing Tally integrations first

Before writing any Tally XML code, vendor and study these open-source projects:
- `tally-mcp` (PyPI: `pip download tally-mcp --no-deps`) — Python, ~40 tools over XML-over-HTTP: reports, masters, voucher creation, bulk import.
- https://github.com/taxor-ai/tally-mcp — Go MCP server, voucher creation with auto-numbering.
- Search GitHub for "tally prime mcp" and "tallyprime xml" for Node variants.

Rules:
- Copy into `vendor/` with LICENSE files preserved (all are MIT/Apache; verify and record in docs/DECISIONS.md).
- Reuse their XML request/response envelopes, collection names, field mappings, and quirk handling as the basis for `packages/tally/xml/`. Port Go/Node logic to Python where needed; do not reimplement from Tally docs what they already got working.
- Where they conflict, prefer the implementation with tests or the most recent commits; note the choice.
- Extend, don't fork blindly: our adapter must add idempotency, ValidationReport, MASTERID-based alter, structured LINEERROR parsing, and the AccountingBackend protocol, which none of them have.
- Optionally expose the upstream `tally-mcp` server as a thin compatibility mode so users already using it can migrate.

## Stack

- Python 3.12, `uv` for env, `pyproject.toml` workspace.
- FastAPI for the daemon HTTP API; `httpx` for Tally XML; `pydantic` v2 for all schemas; `sqlite` via `sqlmodel` for local state (masters cache, idempotency, approval queue, audit log, memory).
- MCP server via the official `mcp` Python SDK (stdio and streamable-HTTP transports).
- WhatsApp channel via Meta Cloud API webhooks (implement adapter + signature verification; make provider pluggable, add a Twilio adapter skeleton).
- Windows tray/desktop app: `pystray` + a small local web UI (HTMX + Tailwind CDN) served by the daemon for approvals and chat. No Electron.
- Vision/OCR: send images to the model's multimodal endpoint; local fallback `rapidocr-onnxruntime`.
- Screen capture: `mss`; window enumeration via `pywin32` on Windows, no-op on other OSes.
- Tests: `pytest`, `pytest-asyncio`, `respx` for HTTP mocking. Include a fake Tally XML server fixture that speaks the real request/response envelope.
- Lint/type: `ruff`, `mypy --strict` on `packages/core`.
- Packaging: `pyinstaller` spec for a single Windows `.exe` daemon; Dockerfile for the MCP/HTTP server mode.

## Repo layout (create all of it)

```
tallyagent/
  pyproject.toml            # uv workspace
  README.md                 # setup for CA firms: enable Tally server mode, install, connect WhatsApp
  docs/DECISIONS.md
  docs/ARCHITECTURE.md      # tiers, data flow, trust boundaries, what leaves the machine
  docs/SECURITY.md
  packages/
    core/                   # domain, no I/O
      tallyagent_core/
        models/             # Ledger, Group, Party, Voucher, VoucherLine, GSTDetails, Company, Period
        validation/         # rules engine: ledger exists, debit==credit, GST rate valid, period open, duplicate detection
        idempotency.py
        policy.py           # action-type -> approval mode (manual | auto | auto_below_amount)
        errors.py
    tally/                  # TallyPrime XML adapter
      tallyagent_tally/
        client.py           # async httpx, connection probe, version detect, company list
        xml/                # request builders + parsers: export (Collection/Report), import (Vouchers, Masters)
        backend.py          # implements AccountingBackend protocol
        fake_server.py      # test double speaking Tally XML
    backends/
      accounting_backend.py # Protocol: list_companies, get_masters, get_report, create_voucher, alter_voucher, get_outstanding, get_bank_ledger
      zoho_books/           # skeleton raising NotImplementedError with clear message
    tools/                  # typed agent tools built on backend
      tallyagent_tools/
        vouchers.py         # create_sales_voucher, create_purchase_voucher, create_payment, create_receipt, create_journal
        masters.py          # create_ledger, create_party, list_ledgers, resolve_ledger_alias
        reports.py          # trial_balance, outstanding_receivables, outstanding_payables, cash_position, day_book, gstr1_data, gstr2_purchase_register
        reconcile.py        # bank_reco(statement_rows), gstr2b_vs_purchase_register(gstr2b_json)
        ingest.py           # invoice_image -> structured voucher draft; bank_statement_pdf -> rows
        registry.py         # tool schema registry (JSON schema for LLM + MCP)
    llm/                    # provider-agnostic
      tallyagent_llm/
        provider.py         # Protocol: complete(messages, tools, images) -> ToolCalls|Text
        deepseek.py         # default, OpenAI-compatible, model=deepseek-flash, prefix-cache friendly ordering
        anthropic_.py
        openai_.py
        router.py           # picks provider from config; tracks token/cost per call
    agent/                  # runtime
      tallyagent_agent/
        loop.py             # plan -> tool calls -> validate -> queue/execute -> respond; max steps; structured tracing
        memory.py           # per-company: ledger aliases, party aliases, recurring patterns, learned corrections
        context.py          # builds minimal prompt context; enforces data-minimisation and logs egress
        tiers.py            # Tier router: API (1) | perceive (2) | computer_use_fallback (3)
        perception/
          screen.py         # periodic screenshot of active Tally window, vision -> {company, screen, period, voucher_type}
        fallback/
          computer_use.py   # gated; only enabled by policy; every action logged; mutations require approval
    approvals/
      tallyagent_approvals/
        queue.py            # pending actions, diff rendering (before/after ledger impact), approve/reject/edit
        audit.py            # append-only log with hash chain
    channels/
      whatsapp/             # Meta Cloud API webhook, media download, reply templates, per-number -> company mapping, auth by allowlist
      twilio_whatsapp/      # skeleton
      web/                  # HTMX chat + approvals UI
    mcp_server/             # exposes tools registry over MCP; reads only by default, writes behind approval
    daemon/                 # FastAPI app wiring everything; tray icon; config loading; health; scheduler (bank reco nightly, GSTR-2B monthly)
  config/
    config.example.toml     # tally host/port, model provider, policy, channels, allowlists
    policy.example.toml
  scripts/
    dev_fake_tally.py       # run fake Tally locally
    seed_demo_company.py
    build_windows.ps1
  tests/                    # unit + integration against fake Tally; >80% coverage on core, tally, tools, approvals
```

## Key behaviours to implement fully

### Tally XML adapter
- Probe: GET `/` on port; parse version; list companies via `<EXPORTDATA>` of `List of Companies`.
- Masters export: ledgers with parent group, GST registration, opening balance; parties with GSTIN, state, credit period.
- Reports: Trial Balance, Day Book (date range), Bills Receivable/Payable (outstanding), Ledger Vouchers, Bank ledger entries, GSTR-1/GSTR-2 relevant collections.
- Import: Sales, Purchase, Payment, Receipt, Journal, Contra vouchers with GST ledgers and inventory-less lines. Parse `<LINEERROR>`/`<EXCEPTIONS>` and surface structured errors.
- Alter voucher by `MASTERID`/`GUID`.
- Handle Tally quirks: date format `YYYYMMDD`, amount sign conventions (credit positive/negative per voucher type), `ISDEEMEDPOSITIVE`, character escaping, company name with special chars, educational-mode date restrictions.

### Validation rules (deterministic, before any write)
- All ledgers resolve to existing masters (with alias memory + fuzzy suggestion; never auto-create silently).
- Debits equal credits to the paisa.
- GST: rate in {0, 0.1, 0.25, 3, 5, 12, 18, 28}; CGST+SGST vs IGST chosen by party state vs company state; GSTIN checksum valid.
- Voucher date within open period and not in a locked/closed month per config.
- Duplicate detection: same party + invoice number + amount within window.
- Return a `ValidationReport` with pass/fail per rule; failures block enqueue unless user overrides with reason (logged).

### Approval queue and diff
- Each pending action renders: natural-language summary, ledger-impact table (debit/credit per ledger), raw XML that will be sent, validation report, source (WhatsApp image / chat / scheduler).
- Approve, reject with reason, edit-and-approve. Edits feed `memory.learned_corrections`.
- Policy promotion: an action type moves to auto-approve only if `policy.toml` says so; provide a CLI to show approval stats per action type to support that decision.

### Agent loop
- System prompt: role, company context (name, FY, state, GST registration), tool list, constraints (never invent ledgers, always propose then confirm for writes).
- Tool-call loop with max 12 steps, structured trace per step (input, tool, output hash, tokens, latency, egress bytes).
- Multimodal ingest: invoice image -> extract {vendor, GSTIN, invoice no, date, line items, taxable value, tax split, total} -> map to voucher draft -> validation -> queue.
- Bank reco: statement rows vs bank ledger entries; match by amount+date window+narration similarity; propose receipts/payments for unmatched; never auto-post.
- GSTR-2B reco: compare uploaded GSTR-2B JSON against purchase register; classify matched / missing in books / missing in 2B / value mismatch; produce CSV + summary.
- Queries: outstanding by party, ageing buckets, cash position, top debtors, last N transactions with a party; answer with numbers pulled from tools, never from model memory.

### Tier 2 perception (read-only)
- Every N seconds (configurable, default off), capture active Tally window, ask the model to classify current screen and extract company/period/voucher type; store as `ui_context` used to ground chat answers ("the report you're viewing shows...").
- Never send screenshots that contain data outside the Tally window; crop to window bounds.

### Tier 3 fallback (gated)
- Disabled by default. When enabled and Tier 1 has no tool for the task, run a bounded computer-use loop (screenshot -> propose keypress/click -> execute via `pyautogui`) with hard step limit, full recording, and mandatory approval for any step that could mutate. Emit a `fallback_used` event so the team knows what to build a proper tool for.

### WhatsApp channel
- Webhook verification, HMAC signature check, media download, image/PDF -> ingest tool, text -> agent loop.
- Number allowlist mapped to company; unknown numbers get a fixed refusal.
- Replies: voucher confirmation with amount/ledgers/approval link; outstanding lists as compact text; errors in plain language.

### MCP server
- Expose every tool in `registry.py` with JSON schemas. Read tools execute directly. Write tools return an approval-queue ticket, not a mutation. Transport: stdio and streamable-HTTP with bearer token.

### Observability and security
- Structured JSON logs; per-request egress log (bytes, fields, destination).
- Append-only audit log with SHA-256 hash chain; `tallyagent audit verify` CLI.
- Secrets from env or OS keyring, never in config files.
- `docs/SECURITY.md`: threat model (prompt injection via invoice images, malicious WhatsApp media, LAN exposure of port 9000), mitigations implemented.

## Deliverables checklist (verify each before finishing)

- [ ] `uv sync && uv run pytest` passes; coverage report printed.
- [ ] `uv run tallyagent probe` detects fake Tally and lists demo company.
- [ ] `uv run tallyagent chat` REPL works against fake Tally with `deepseek-flash` (read key from `DEEPSEEK_API_KEY`; if absent, run with a deterministic mock provider and say so).
- [ ] Posting a sales voucher from a sample invoice image lands in the approval queue with a correct ledger-impact diff; approving posts to fake Tally; re-submitting the same image is a no-op via idempotency.
- [ ] Bank reco and GSTR-2B reco produce correct classifications on the fixtures in `tests/fixtures/`.
- [ ] MCP server starts and lists tools; a write tool returns a ticket instead of mutating.
- [ ] WhatsApp webhook handles a signed test payload end to end.
- [ ] Windows build script and Dockerfile present and documented.
- [ ] `docs/ARCHITECTURE.md` includes a diagram of the three tiers and the trust boundary.
- [ ] `docs/DECISIONS.md` lists every assumption you made.

Work in this order: core models + validation -> Tally XML client + fake server -> tools -> approvals + audit -> LLM providers -> agent loop -> MCP server -> web UI -> WhatsApp -> perception -> fallback -> packaging -> docs. Commit after each stage with a descriptive message. When done, print a summary of what works, what is skeleton, and the exact commands to run the demo.

## Auto-mode constraints
- Never connect to any Tally instance other than the fake server in tests. Default config must point Tally host to a non-routable placeholder until the user edits it.
- Never send real data to any LLM API during the build. Use the mock provider for all tests; only use DEEPSEEK_API_KEY in the final smoke test with synthetic fixtures.
- Do not install system packages, modify files outside the repo, or open ports other than the daemon's local port in tests.
- If a stage cannot be completed, write a stub that raises NotImplementedError with a clear message, log it in docs/DECISIONS.md under "Incomplete", and continue. Never silently fake a passing test.
- Keep total dependency count minimal; no packages outside pyproject.toml.