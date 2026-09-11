# tallyagent

Agentic bookkeeping for **TallyPrime**. It reads and writes through Tally's
XML-over-HTTP interface, validates every voucher against your masters before it
goes anywhere, and queues every mutation for a human to approve.

Nothing posts to Tally unless a person approves it.

---

## What it does

- **Ask questions in plain language.** Outstanding by party with ageing buckets,
  cash position, top debtors, the day book, the last N transactions with a
  party. Every figure comes from a tool call against Tally — never from the
  model's memory.
- **Draft vouchers.** Sales, purchase, payment, receipt, journal, and amendments
  by `MASTERID`. GST is split CGST+SGST or IGST from the party's state against
  the company's, not from whatever a document claimed.
- **Read invoices.** Send a photo over WhatsApp; it becomes a validated draft in
  the approval queue with a ledger-impact diff.
- **Reconcile.** Bank statement against the bank ledger, and GSTR-2B against the
  purchase register, classified as matched / missing in books / missing in 2B /
  value mismatch, with a CSV. Both propose; neither posts.
- **Approve, reject, or edit.** The approval screen shows the ledger impact, the
  validation report rule by rule, and the exact XML that will be sent. Edit a
  ledger and it remembers the correction.
- **Serve tools over MCP.** Reads run; writes return an approval ticket.

## Try it in two minutes, without TallyPrime

```bash
uv sync --extra dev
cp config/config.example.toml config/config.toml
cp config/policy.example.toml config/policy.toml

uv run tallyagent probe --fake-tally     # detects the fake Tally, lists the demo company
uv run tallyagent chat  --fake-tally     # a REPL; try "what is outstanding?"
uv run tallyagent serve --fake-tally     # the web UI at http://127.0.0.1:8787
```

Without a model API key it runs on a deterministic mock provider and says so.
For the real thing, `export DEEPSEEK_API_KEY=...` (or `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` with `[model] provider` changed to match).

To have data to look at:

```bash
uv run python scripts/seed_demo_company.py
```

---

## Setup for a CA firm

### 1. Turn on Tally's server mode

In TallyPrime: **F1 → Settings → Connectivity → Client/Server configuration**

- *TallyPrime acts as*: **Both** (or **Server**)
- *Enable ODBC*: Yes
- *Port*: **9000**

Keep TallyPrime open with the company loaded — it only answers for open
companies.

> **Bind it to localhost, or firewall port 9000 to the machine running
> tallyagent.** Tally's XML interface has no authentication: anything that can
> reach that port can read and write your clients' books. See
> [docs/SECURITY.md](docs/SECURITY.md).

### 2. Install

```bash
git clone <this repo> && cd tallyagent
uv sync --extra dev --extra desktop --extra mcp
cp config/config.example.toml config/config.toml
cp config/policy.example.toml config/policy.toml
```

Or build a single Windows executable:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# → dist\tallyagent.exe
```

### 3. Configure

Edit `config/config.toml`. The three that matter:

```toml
[tally]
host = "127.0.0.1"          # ships as 0.0.0.0, a placeholder that refuses to connect
port = 9000
company = "Your Client Pvt Ltd"

[company]
state_code = "27"           # your GST state; drives IGST vs CGST+SGST
gstin = "27AAPFU0939F1ZV"
fy_start = "2026-04-01"
fy_end = "2027-03-31"
locked_before = "2026-04-01"   # nothing may be posted before this date

[model]
provider = "deepseek"       # deepseek | anthropic | openai | mock
model = "deepseek-flash"
```

**Secrets never go in this file.** Set them in the environment (or the OS
keyring, under service name `tallyagent`):

| Variable | For |
|---|---|
| `DEEPSEEK_API_KEY` | the default model provider |
| `TALLY_PASSWORD` | Tally, if your installation requires one |
| `WHATSAPP_APP_SECRET` | verifying webhook signatures |
| `WHATSAPP_ACCESS_TOKEN` | sending replies and fetching media |
| `TALLYAGENT_MCP_TOKEN` | the MCP HTTP transport |

### 4. Run

```bash
uv run tallyagent probe     # confirm Tally is reachable and the company is open
uv run tallyagent serve     # web UI + tray icon at http://127.0.0.1:8787
```

### 5. Connect WhatsApp (optional)

1. Create a Meta app with the **WhatsApp** product, and note the phone number ID.
2. Expose the daemon over HTTPS (a tunnel or a reverse proxy). The webhook URL is
   `https://<your-host>/whatsapp/webhook`.
3. In `config.toml`:

```toml
[channels.whatsapp]
enabled = true
verify_token = "pick-any-string"     # must match what you enter in the Meta console
phone_number_id = "123456789012345"

[channels.whatsapp.allowlist]
"+919876543210" = "Your Client Pvt Ltd"
```

4. `export WHATSAPP_APP_SECRET=...` and `WHATSAPP_ACCESS_TOKEN=...`, then restart.
   The daemon refuses to mount the webhook without the app secret.

Numbers not in the allowlist get a fixed refusal. One number maps to one company.

### 6. Connect an MCP client (optional)

```bash
uv run tallyagent mcp --transport stdio          # for a desktop MCP client
TALLYAGENT_MCP_TOKEN=... uv run tallyagent mcp --transport http
```

Write tools return an approval ticket rather than mutating. `--read-only` hides
them entirely.

---

## How it decides what to trust

Three tiers, described fully in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md):

| Tier | What | Default |
|---|---|---|
| 1 | Tally's XML API through typed tools. The only write path. | always |
| 2 | Screenshot of the Tally window, classified for context. Read-only. | **off** |
| 3 | Bounded computer use when no tool exists. Every mutating step needs approval. | **off** |

Before any write: ledgers must resolve to existing masters (a near match is
*suggested*, never substituted), debits must equal credits to the paisa, the GST
rate must be notified and split correctly, the GSTIN must checksum, the date
must be in an open period, and it must not duplicate a voucher already posted.
Failures block the queue unless a human overrides with a reason, which is logged.

Every write carries an idempotency key derived from the voucher's content, so
the same invoice submitted twice is a no-op rather than double turnover.

## Operating it

```bash
uv run tallyagent approvals list             # what is waiting
uv run tallyagent approvals stats            # per action type, to inform policy
uv run tallyagent approvals approve APR-0001 --actor you@firm.in
uv run tallyagent approvals reject  APR-0002 --actor you@firm.in --reason "wrong party"
uv run tallyagent audit verify               # check the hash chain
uv run tallyagent audit log --limit 50
```

Auto-approval is opt-in per action type in `config/policy.toml`. Nothing is
auto-approved out of the box, and nothing promotes itself — `approvals stats`
gives you the evidence; the edit is yours.

## Running in a container

```bash
docker build -t tallyagent .
docker run --rm -p 8787:8787 \
  -v "$PWD/config:/app/config:ro" -v tallyagent-data:/data \
  -e DEEPSEEK_API_KEY -e TALLYAGENT_MCP_TOKEN \
  tallyagent serve --config config/config.toml
```

Point `[tally] host` at the Windows machine running TallyPrime. There is no tray
icon and no screen perception in a container; Tier 3 is unavailable by
construction.

## Development

```bash
uv sync --extra dev --extra mcp
uv run pytest -q                                   # the whole suite, against the fake Tally
uv run pytest --cov=packages --cov-report=term-missing
uv run ruff check packages tests scripts
uv run mypy                                        # strict, on packages/core

uv run python scripts/dev_fake_tally.py            # a fake Tally on a real port
```

Tests never touch a real Tally and never call a model API.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the three tiers, the data flow,
  the trust boundary, and what crosses it.
- [docs/SECURITY.md](docs/SECURITY.md) — threat model and mitigations.
- [docs/DECISIONS.md](docs/DECISIONS.md) — every assumption made while building
  this, and what is incomplete.
- [docs/PLAN.md](docs/PLAN.md) — the build plan, stage by stage.
- [vendor/README.md](vendor/README.md) — the upstream Tally integrations this
  was built on, with licences.

## Licence

MIT.
