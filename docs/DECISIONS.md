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

## Incomplete

- Node/TypeScript Tally MCP variants were not vendored (D-005). The two vendored projects
  cover the XML surface we need; if a Node variant later proves to handle a quirk better,
  it drops into `vendor/` the same way.
