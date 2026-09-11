# tallyagent — build plan

Confirmation: `SPEC.md` read in full (189 lines, all sections: design principles, reuse
rules, stack, repo layout, key behaviours, deliverables checklist, auto-mode constraints).

Stages execute in the order SPEC.md mandates. One commit per stage,
message `stage N: <name>`.

| # | Stage | Files created | Test that proves it |
|---|-------|---------------|---------------------|
| 0 | scaffold + vendor | `pyproject.toml`, `docs/PLAN.md`, `docs/DECISIONS.md`, `vendor/tally_mcp_pypi/`, `vendor/taxor-tally-mcp/`, `vendor/README.md`, `config/*.example.toml` | `uv sync` resolves; `tests/test_vendor.py` asserts vendored sources + LICENSE present |
| 1 | core models + validation | `packages/core/tallyagent_core/models/*.py`, `validation/*.py`, `idempotency.py`, `policy.py`, `errors.py` | `tests/test_core_models.py`, `tests/test_validation.py`, `tests/test_idempotency.py`, `tests/test_policy.py` — GSTIN checksum, debit==credit, GST rate/split, period lock, duplicate window, idempotency replay |
| 2 | Tally XML client + fake server | `packages/tally/tallyagent_tally/{client.py,backend.py,fake_server.py}`, `xml/{builders.py,parsers.py,quirks.py}` | `tests/test_tally_xml.py`, `tests/test_tally_client.py` — envelope shape, YYYYMMDD dates, ISDEEMEDPOSITIVE signs, LINEERROR parsing, MASTERID alter, probe/list companies against `fake_server` |
| 3 | tools layer | `packages/tools/tallyagent_tools/{vouchers,masters,reports,reconcile,ingest,registry}.py`, `packages/backends/*` | `tests/test_tools_vouchers.py`, `tests/test_tools_reports.py`, `tests/test_reconcile.py`, `tests/test_registry.py`, `tests/test_ingest.py` + `tests/fixtures/` bank + GSTR-2B fixtures |
| 4 | approvals + audit | `packages/approvals/tallyagent_approvals/{queue.py,audit.py,db.py}` | `tests/test_approvals.py`, `tests/test_audit.py` — diff render, approve/reject/edit, hash-chain verify + tamper detection |
| 5 | LLM providers | `packages/llm/tallyagent_llm/{provider,deepseek,anthropic_,openai_,mock,router}.py` | `tests/test_llm_router.py` — provider swap by config, mock determinism, token/cost accounting, `respx`-mocked deepseek call |
| 6 | agent loop | `packages/agent/tallyagent_agent/{loop,memory,context,tiers}.py` | `tests/test_agent_loop.py`, `tests/test_memory.py`, `tests/test_context.py` — max 12 steps, trace per step, egress log, alias learning |
| 7 | MCP server | `packages/mcp_server/tallyagent_mcp_server/server.py` | `tests/test_mcp_server.py` — lists tools; write tool returns approval ticket, no mutation on fake Tally |
| 8 | web UI | `packages/channels/web/{app.py,templates/}` | `tests/test_web_ui.py` — approvals list + approve POST via FastAPI TestClient |
| 9 | WhatsApp | `packages/channels/whatsapp/{webhook.py,meta.py}`, `twilio_whatsapp/` | `tests/test_whatsapp.py` — GET verify challenge, HMAC signature accept/reject, allowlist refusal, signed text payload → agent |
| 10 | perception (Tier 2) | `packages/agent/tallyagent_agent/perception/screen.py`, `tiers.py` wiring | `tests/test_perception.py` — off by default, window-crop bounds, ui_context shape |
| 11 | fallback (Tier 3) | `packages/agent/tallyagent_agent/fallback/computer_use.py` | `tests/test_fallback.py` — disabled by default raises, step cap, mutation requires approval, `fallback_used` event |
| 12 | packaging | `scripts/build_windows.ps1`, `tallyagent.spec`, `Dockerfile`, `scripts/dev_fake_tally.py`, `scripts/seed_demo_company.py` | `tests/test_packaging.py` — files exist and are referenced from README |
| 13 | docs + deliverables verify | `README.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/DECISIONS.md` final | full `uv run pytest --cov`; deliverables checklist printed with pass/fail |
