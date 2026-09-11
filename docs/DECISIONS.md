# Decisions and assumptions

Every assumption made while building `tallyagent` in auto-mode. Newest stage last.

## Stage 0 — scaffold and vendoring

- **D-001 Single distribution, not a uv workspace of 10 packages.** SPEC.md asks for a
  `uv` workspace. Ten sub-`pyproject.toml` files buy nothing here (all ship together,
  all version together) and cost ten files of churn. Instead: one root `pyproject.toml`
  with a hatchling `packages = [...]` list naming every `packages/<x>/tallyagent_<x>`
  directory, so the on-disk layout matches SPEC.md exactly and imports are flat
  (`tallyagent_core`, `tallyagent_tally`, ...). Splitting later is a mechanical change.
- **D-002 Python `>=3.12`.** SPEC.md says 3.12; the dev machine has 3.13.7. Pinned as a
  floor, not an equality, so both work.
- **D-003 Heavy/OS-specific deps are optional extras.** `mcp`, `pystray`/`mss`/`pywin32`,
  `rapidocr-onnxruntime`, `pyautogui` sit in `[project.optional-dependencies]`
  (`mcp`, `desktop`, `ocr`, `fallback`). Core install and the whole test suite need none
  of them. Keeps `uv sync` fast and keeps a Linux/Docker install viable. Modules that
  need them import lazily and raise a clear message if missing.
- **D-004 `lxml` over stdlib `ElementTree`.** The vendored `tally-mcp` XML cleanup relies
  on lxml behaviours (namespace injection, `pretty_print`), and Tally's malformed output
  is exactly where a lenient parser earns its dependency.
- **D-005 Vendored sources and licences.**
  - `vendor/tally_mcp_pypi/` — PyPI `tally-mcp` 0.2.0, MIT (declared in wheel METADATA
    via `License-Expression: MIT`; no LICENSE text file shipped — noted in
    `vendor/tally_mcp_pypi/LICENSE.NOTE`).
  - `vendor/taxor-tally-mcp/` — github.com/taxor-ai/tally-mcp at `d1fb008` (2026-04-23),
    MIT declared in README only, no LICENSE file upstream — noted in
    `vendor/taxor-tally-mcp/LICENSE.NOTE`.
  - No Node variant was vendored: GitHub search for "tally prime mcp"/"tallyprime xml"
    was not reachable from the build sandbox beyond the two direct fetches above. Logged
    under *Incomplete*.
- **D-006 What we took from each vendored project.** `tally-mcp` (Python) is the primary
  basis: its `ENVELOPE/HEADER/BODY/DESC/STATICVARIABLES` export envelope, its
  `IMPORTDATA/REQUESTDESC/REQUESTDATA/TALLYMESSAGE` import envelope, its response cleanup
  (stripping invalid control chars and bad `&#N;` charrefs, injecting the undeclared
  `xmlns:UDF`, latin-1 re-encode on broken UTF-8), its `ISDEEMEDPOSITIVE`/negated-amount
  convention, and its "no `OBJVIEW` attribute — it crashes Tally with c0000005" quirk.
  From `taxor-ai/tally-mcp` (Go) we took the error taxonomy shape
  (Connection / Tally / Validation errors as distinct types) and the idea of a declarative
  tool registry; its Go template renderer was not ported (we build XML with lxml, which
  escapes correctly by construction — string templating is how XML injection happens).
- **D-007 Conflict resolution.** Where the two disagreed, the Python package won: it has
  the quirk comments tied to observed Tally crashes and is the same language as this
  codebase. Noted per SPEC.md's "prefer the implementation with tests or the most recent
  commits" — the Go repo is more recently committed, but its Tally-behaviour surface is
  thinner (templates + XPath specs) than the Python one's hard-won quirk handling.
- **D-008 What we add that neither has** (per SPEC.md): client-generated idempotency keys
  persisted per voucher, a deterministic `ValidationReport` gate before any write,
  `MASTERID`-based alter, structured `<LINEERROR>`/`<EXCEPTIONS>` parsing into typed
  errors, and the `AccountingBackend` protocol.
- **D-009 Default config points Tally at `0.0.0.0`**, a deliberately non-routable
  placeholder, per the auto-mode constraint. Tests only ever talk to `fake_server`.
- **D-010 Receipts are the only action shipped with a non-manual policy example**
  (`auto_below_amount`, ₹5000) and only in `policy.example.toml` — the shipped default
  for an unlisted action type is `manual`.

## Stage 1 — core

- **D-011 Money is `Decimal`, never `float`.** Every amount, rate and balance.
  `PAISA = Decimal("0.01")` is the single quantisation constant.
- **D-012 Our sign convention is the plain-English one:** positive = debit,
  negative = credit, on `VoucherLine.amount`. Tally's convention (negated
  `<AMOUNT>` plus `ISDEEMEDPOSITIVE`) is applied only at the XML boundary, in
  one function (`quirks.tally_amount`). Mixing the two is the classic source of
  reversed vouchers.
- **D-013 A voucher's fingerprint is line-order independent.** It sorts lines
  before hashing, so the same economic voucher built two ways collides. It feeds
  both duplicate detection and the idempotency key.
- **D-014 Fuzzy matching suggests, never substitutes.** `difflib` is used to
  offer candidates for an unknown ledger; nothing applies one automatically.
  A silently-substituted ledger books revenue against the wrong customer.
- **D-015 Validation severity has two levels that matter.** ERROR blocks the
  queue; WARNING is shown to the approver and does not. "Place of supply
  unknown" and "no financial period configured" are warnings, because blocking
  on them would stop a firm using the system at all before it is fully set up.
- **D-016 A rule that raises becomes a failing rule, not a skipped one.**
  A validator that crashes must never silently become a validator that is absent.
- **D-017 Intra-state CGST and SGST may differ by one paisa.** Halving an odd
  tax amount legitimately leaves a paisa on one side; the rule allows exactly
  that and no more.
- **D-018 An overridden validation failure requires a written reason.** Enforced
  in `ValidationReport.override`, and the reason goes to the audit log.

## Stage 2 — Tally adapter

- **D-019 XML is built with lxml element construction, never string
  concatenation.** The vendored projects template strings; a ledger named
  `Smith & Co` breaks that, and string templating is how XML injection happens.
- **D-020 `<VOUCHER>` never carries an `OBJVIEW` attribute.** Taken from the
  vendored package's comment tying it to a Tally crash (c0000005 access
  violation). Recorded in `quirks.NEVER_EMIT_ATTRIBUTES` so it is not re-added.
- **D-021 Tally's HTTP status is ignored as a success signal.** It answers 200
  for logical failures; success is decided by parsing `<IMPORTRESULT>` and
  `<LINEERROR>`.
- **D-022 `LineError.kind` classifies free text** into `unknown_master`,
  `duplicate`, `period` and `unknown`. String matching, deliberately: Tally
  gives no error codes, and the fix differs per kind.
- **D-023 The alter attribute is chosen by id length** — `MASTERID` for short
  numeric ids, `REMOTEID` for GUIDs.
- **D-024 The fake Tally holds real state and reproduces real failures.** Post a
  voucher and its trial balance moves; an unknown ledger and a duplicate voucher
  number come back as genuine `<LINEERROR>` responses. A fake that only succeeds
  tests nothing.
- **D-025 Tests attach `httpx.MockTransport`, never a socket.** The auto-mode
  constraint "never connect to any Tally instance other than the fake server" is
  therefore structural rather than a convention.

## Stage 3 — tools

- **D-026 Every voucher write goes through `base.submit`.** validate → block on
  failure → auto-approve only if policy says so → otherwise queue. There is no
  other path from a tool to `backend.create_voucher`.
- **D-027 The approval queue is injected as a callable, not imported.** The
  tools package does not depend on the approvals package, which keeps the
  dependency direction one-way and lets the MCP server run without a queue (and
  refuse writes when it does).
- **D-028 GST ledger names are a convention, not a discovery.** Output/Input
  CGST/SGST/IGST as seeded in the demo company. A firm with a different chart
  configures them; nothing invents a tax ledger at runtime.
- **D-029 Reconciliation matches amounts exactly and dates within a window.**
  Narration similarity only breaks ties. Two different payments of similar size
  on the same day must never be collapsed into one match.
- **D-030 Reconciliation proposes and never posts** — including from the
  scheduler. An unattended process writing to a client's books is not a feature.
- **D-031 A bank narration is matched to a party by word containment first,**
  then by fuzzy ratio. "NEFT ACME INDUSTRIES ADV" contains every significant
  word of "Acme Industries"; whole-string similarity alone misses it.
- **D-032 Invoice extraction derives the GST rate** from the tax and the taxable
  value rather than trusting a printed rate, snapping to the nearest notified
  rate within 0.15.
- **D-033 An unparseable extraction produces warnings, not an exception.** The
  draft still reaches a human, who can see what was read and what was not.
- **D-034 Bank statement PDFs are parsed as text with a regex,** not with a PDF
  table library. Indian bank statement layouts vary wildly; a row that does not
  match is dropped and reported rather than silently mis-parsed. No PDF parsing
  dependency enters the tree.
- **D-035 Tool JSON Schemas are hand-written,** not derived from signatures.
  They are the prompt the model reads, so the wording is load-bearing.

## Stage 4 — approvals and audit

- **D-036 SQLite via sqlmodel, one file, all local state:** approvals, audit
  chain, idempotency, memory, egress. Nothing here is ever sent to a model.
- **D-037 Idempotency records are written before the request leaves,** and a
  `pending` record blocks a concurrent duplicate. A previously *failed* attempt
  may be retried; a *succeeded* one never re-runs.
- **D-038 Audit hashing uses canonical JSON** (sorted keys, fixed separators) so
  a re-serialised identical payload hashes identically and does not look like
  tampering.
- **D-039 The audit log is tamper-evident, not tamper-proof.** Someone with write
  access to the file can rebuild the chain. Stated plainly in `docs/SECURITY.md`
  rather than implied away.
- **D-040 A rejection requires a reason; an approval does not.** An unexplained
  rejection teaches nobody anything; an approval is itself the explanation.
- **D-041 Edit-and-approve issues a new idempotency key,** because a different
  voucher is a different write. Ledger substitutions are learned positionally as
  aliases.
- **D-042 `ApprovalStats.recommendation` is advice only** and needs 20+ decisions
  before it says anything. Promotion to auto-approve is a human edit to
  `policy.toml`; a system that promotes itself is not a control.

## Stage 5 — model layer

- **D-043 The OpenAI provider inherits the DeepSeek transport** because the APIs
  are byte-compatible. Anthropic does not: its system prompt is a top-level
  field and tool results are user-turn content blocks, so it is written out.
- **D-044 Prompt ordering is fixed** — system, then history, then the new turn —
  so DeepSeek's prefix cache sees a stable prefix. A prompt that reshuffles its
  own preamble never gets a cache hit.
- **D-045 The mock provider answers with real tool calls,** driven by a script or
  a small rule set. It is what lets the entire suite exercise the production
  agent loop with no key and no network.
- **D-046 Cost figures are estimates from a small static price table.** A stale
  entry makes the estimate less useful, never wrong in a way that changes
  behaviour.
- **D-047 The egress log records message *shapes*, never content** — role, image
  count, byte count, tokens, cost. It must be safe to keep and safe to hand to a
  client's IT department.

## Stage 6 — agent

- **D-048 The system prompt carries company metadata only:** name, FY, state
  code, GSTIN. Never balances, never the party list. Data minimisation is a
  property of what is *built*, not of what is redacted afterwards.
- **D-049 Tool results are truncated to 4000 characters** before they are sent,
  with a message telling the model to narrow the range. The full result stays in
  the local trace.
- **D-050 A tool failure becomes a message, not an exception.** The model needs
  to be told what went wrong so it can correct itself; a raised exception ends
  the conversation instead.
- **D-051 Hitting the step limit is reported honestly,** naming the tools tried.
  Silently returning a partial answer as if it were complete is worse than
  saying it stopped.
- **D-052 Memory is only what a human taught by correcting.** No inference from
  data the user did not act on; every entry inspectable and deletable.

## Stage 7 — MCP

- **D-053 Write tools over MCP return a ticket, never a mutation** — and a write
  tool with no queue wired refuses rather than falling through to the backend
  under policy.
- **D-054 Read-only mode hides write tools rather than listing-and-refusing
  them.** A tool that is advertised and always fails wastes a client's turn.
- **D-055 The MCP HTTP bearer check is ASGI middleware,** so it runs before a
  single MCP frame is parsed, and the transport refuses to start without a token.
- **D-056 The SDK is an optional extra and is used through the low-level
  `Server`** with `on_list_tools`/`on_call_tool`, not the tool-decorator API,
  because our schemas must not be re-derived from Python signatures. Everything
  but the transport binding works and is tested without the SDK installed.

## Stage 8-9 — channels

- **D-057 One `Services` object is shared by every channel.** Channels differ in
  transport and in who is speaking; they must not differ in what they may do.
- **D-058 An edit that unbalances a voucher is refused in the UI** before it
  reaches the queue, and a partial edit form keeps the original lines rather
  than zeroing them.
- **D-059 WhatsApp order is signature → allowlist → content.** The HMAC check
  runs before the body is parsed as JSON; an unlisted number's text is never
  read as instructions.
- **D-060 Meta retries are deduplicated on message id,** so a retried webhook
  cannot produce a second draft.
- **D-061 Media is capped at 8 MB** and PDFs are treated as text, keeping every
  file-format parser out of the dependency tree.

## Stage 10-11 — tiers

- **D-062 Screen capture is cropped to the Tally window's rectangle,** matched by
  window title, and a minimised or off-screen window is refused. The user's
  email and their other clients' files are not ours to send anywhere.
- **D-063 An unreadable screen classification yields an empty `ui_context`,** not
  a guess. A wrong one would silently mis-ground every subsequent answer.
- **D-064 In Tier 3, every click counts as mutating,** along with a deliberately
  broad keystroke list. A false positive costs one approval click; a false
  negative costs a wrong voucher.
- **D-065 No approval hook means no mutating step ever executes.** Defaulting to
  "allow" would quietly undo the whole gate.
- **D-066 Tier 3 frames stay local;** only their byte sizes enter the recording.

## Stage 12 — daemon and packaging

- **D-067 Secrets are never read from the config file,** even if someone writes
  them there. There is a test asserting a `password` in `config.toml` is ignored.
- **D-068 A missing config file yields defaults rather than an error,** so the
  tray app still starts and can tell the user what to fill in.
- **D-069 A missing API key falls back to the mock provider, loudly.** A firm
  trying the demo gets a working, obviously-fake agent instead of a stack trace,
  and both the CLI and the UI say which is running.
- **D-070 `Company.name` may be empty.** A freshly installed daemon has no
  company configured and must be able to start and say so.
- **D-071 `--fake-tally` rewrites the reported host to `fake-tally`,** so `probe`
  never prints the placeholder address as though it had connected to it.
- **D-072 Egress is written to two sinks:** SQLite for the UI, JSONL for handing
  to a client's IT department. A full disk warns and does not stop the agent.
- **D-073 The Windows build script runs the test suite before PyInstaller,** and
  smoke-tests the built exe against the fake Tally.
- **D-074 The Docker image runs as an unprivileged user** and carries a
  healthcheck. Tier 2 and Tier 3 are unavailable in a container by construction.

## Stage 13 — verification

- **D-075 Approver-facing figures are quantised to the paisa.** A diff rendering
  `5000.0` in front of an accountant reads as a bug in the numbers.
- **D-076 A voucher's value for duplicate detection is the sum of its debit
  lines,** not the largest one. On a purchase the party sits on the credit side
  and the debits split across expense and tax, so taking the largest line
  understated the voucher and missed the duplicate. Found by the end-to-end test.

## Incomplete

Nothing raises `NotImplementedError` except the two deliberate skeletons below.
These are the known gaps.

- **Node/TypeScript Tally MCP variants were not vendored** (D-005). The two
  vendored projects cover the XML surface we need; if a Node variant later proves
  to handle a quirk better, it drops into `vendor/` the same way.
- **The Zoho Books backend is a skeleton.** Every method raises
  `NotImplementedError` naming the protocol to implement and pointing at the
  Tally adapter as the worked example. Deliberate, per SPEC.md.
- **The Twilio WhatsApp provider is a skeleton.** Raises with a description of
  exactly how Twilio's webhook differs from Meta's (SHA-1 over the URL plus
  sorted parameters, form-encoded body, `MediaUrl0..N` behind basic auth).
  Deliberate, per SPEC.md.
- **The upstream `tally-mcp` compatibility mode was not built.** SPEC.md lists it
  as optional ("Optionally expose the upstream tally-mcp server as a thin
  compatibility mode"). Our MCP server exposes the same surface under our own
  tool names with the approval gate added; a name-for-name shim to the upstream
  tool names would be a mapping table over `registry.py` if someone needs it.
- **Local OCR is an opt-in extra, not a wired-in fallback.**
  `rapidocr-onnxruntime` is declared under `[project.optional-dependencies] ocr`
  and named in SECURITY.md, but `ingest.py` does not call it: the default path
  sends bytes to the multimodal endpoint, and adding a local image parser is a
  decision a firm should make deliberately rather than get by default.
- **PDF text extraction is regex-over-decoded-bytes** (D-034). A PDF whose text
  is not extractable that way is reported as unreadable with advice to export
  CSV, rather than guessed at.
- **Tier 3 has no UI.** `ComputerUseFallback` takes an approval hook; the daemon
  does not wire one, so with the fallback enabled but no hook supplied a mutating
  step stops the session. That is the safe failure, but it means Tier 3 is usable
  programmatically and not yet from the web UI.
- **`pyinstaller` and `pystray` were not executed on this machine.** The spec,
  the build script and the Dockerfile are present, documented, and asserted to
  exist and to be correctly shaped; an actual Windows build and an actual
  container build were not run as part of this session.

## Stage 14 — live TallyPrime

Target instance for every finding below: **TallyPrime 1.1.7.1, Educational
(student) mode**, `C:\Program Files\TallyPrime`, probed 2026-09-11.

### What live Tally actually does (measured, not assumed)

- **D-077 The request charset must match the bytes sent, and we were getting it
  wrong.** `client.py` declared `Content-Type: text/xml; charset=utf-16` while
  sending UTF-8. Live Tally answers *every* such request with
  `<RESPONSE>Unknown Request, cannot be processed</RESPONSE>` — no error code,
  no hint, HTTP 200. Declaring `charset=utf-8` makes everything work. The fake
  server never caught this because it ignores headers. Pinned in
  `quirks.REQUEST_CONTENT_TYPE`.
- **D-078 Tally echoes the charset back.** Declare UTF-16 and the *response* is
  UTF-16LE. `clean_response` now detects UTF-16 (BOM or NUL-interleaving) and
  re-encodes to UTF-8, so one parser handles either. `tallyerr.log` is UTF-16LE
  too.
- **D-079 XML company creation is not supported on this build.** Every
  `Import Data` resolves a current company first. With none loaded:
  `<LINEERROR>Could not find Company ''</LINEERROR>`. With an empty
  `SVCURRENTCOMPANY` element: `<LINEERROR>Could not set 'SVCurrentCompany' to
  ''</LINEERROR>`. A `REPORTNAME` of `Create Company` hangs the request until it
  times out. **Company creation therefore requires the Tier 3 keyboard path** —
  which is what Stage 14.1 Attempt B exists for.
- **D-080 `<CMPINFO>` is a free health signal.** Every export response carries
  object counters (`COMPANY`, `LEDGER`, `VOUCHER`, ...). All zero means no
  company is loaded, i.e. Tally is sitting on the Select Company screen. The
  probe reads it instead of inferring from an empty collection.
- **D-081 Malformed TDL crashes Tally outright.** A collection referencing an
  undefined description killed the process (`tally1.dmp` written,
  `tallyerr.log`: "Error in TDL. 'Collection:TACompanies' Could not find
  description!"). Custom TDL is therefore treated as a loaded weapon: the
  adapter sends none by default, and anything that does must be tested against a
  disposable install first.
- **D-082 Tally reports no version over XML.** The GET banner is exactly
  `<RESPONSE>TallyPrime Server is Running</RESPONSE>`. Version comes from the
  `tally.exe` file version instead; edition comes from config, since the banner
  does not name it on 1.x.
- **D-083 The window title carries the port** once server mode is on
  (`TallyPrime:9000`). Used as a cross-check: `tally.ini` says what Tally will do
  next start, the title says what the running process is doing now.

### 14.0 — live-mode safety

- **D-084 Write scope is a prefix check on the company name, and nothing
  cleverer.** Live mode may only write to companies starting with `TA-`
  (configurable). An allow-list or a regex would be a thing to get subtly wrong;
  a prefix is auditable at a glance. Enforced in `livemode.LiveMode`, called at
  the top of `tools.base.submit` and `tools.masters._submit_master` — before
  validation, before the queue, before any XML is built.
- **D-085 An empty `write_prefix` allows nothing, not everything.** Fail closed:
  the misconfiguration that would otherwise mean "write anywhere" is the one
  that must not.
- **D-086 `--fake-tally` forces fake mode.** The guard is about protecting real
  books; leaving it on against the fake would refuse to write to the fake's own
  demo company.
- **D-087 EDU mode defaults ON in live mode.** A student install is the common
  case, and being wrong in that direction costs a warning rather than a voucher
  Tally silently refuses. `tally.edu = false` turns it off for a licensed
  install.
- **D-088 `edu_date_allowed` is a validation rule, not a client-side filter.**
  The failure then reads as a report line naming the dates that would work,
  shown in the approval diff like every other rule.
- **D-089 Date snapping never happens silently.** `snap_to_edu_date` returns
  `(date, warning)` and the warning is always surfaced. A silently moved date is
  a voucher in the wrong period — at a month boundary, the wrong GST return.
- **D-090 `EDU_ALLOWED_DAYS` is duplicated in core and in the adapter** rather
  than imported, because `core` must not depend on `tally`. A test pins the two
  equal.
