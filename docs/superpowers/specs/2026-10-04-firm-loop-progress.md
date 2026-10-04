# Firm loop - build progress

The vision: a reliable, agentic-AI-first automation layer for Tally, sold to CA
firms, positioned as the firm's AI staff member. It runs every client every
morning, reconciles first, and earns autonomy rather than assuming it. The
evidence behind the choice is in `docs/research/2026-10-04-ca-market-and-ai-adoption.md`.

Reliability means hands-off throughput, and the way to get it without losing
the trust conditions CAs set is earned autonomy. Each action type, per client,
moves from gated to hands-off on a measured record and falls back on a miss.

| # | Module | State |
|---|---|---|
| 1 | FirmRunner, Job protocol, Outcome, Firm Inbox, Morning Brief | **done** |
| 2 | Earned autonomy ramp and no-touch metric | next |
| 3 | `reconcile_2b` job: mismatch classification, vendor follow-ups, deferred-ITC ledger, IMS suggestions | |
| 4 | Edit Log edition check, local model option | |
| 5 | Wiring: scheduler, TUI `/inbox`, web home page, CLI `firm run` | |

## 1. Firm loop core - done

`packages/daemon/tallyagent_daemon/firm/`

- `jobs.py`
  - The `Job` protocol: `due(client, today)` and `run(ctx, client, today)`, returning a list of `Outcome`.
  - `Outcome` kinds: done, queued, exception.
  - `MonthEndJob` and `Gstr1Job` wrap the existing tools; nothing is rewritten.
- `runner.py`: `FirmRunner` walks the register.
  - A client health check comes first: is Tally answering, and is the company loaded.
  - One client failing, one job throwing, or one client timing out becomes an exception line, never a stopped run.
  - Jobs never post anything; autonomy is applied in one place, the runner.
- `inbox.py`: one list across every client, stored in the firm database (`firm_inbox`).
  - Ranked: exceptions, then queued, then done; within that, money at risk, then deadline.
  - Keyed by day, client, job and subject, so re-running the same morning updates lines rather than doubling them, and resolved lines stay resolved.
- `brief.py`: the morning brief as markdown, with one row per client and the worst items first, plus a WhatsApp-length version.

Tests: `tests/test_firm_runner.py` (11), against two fake-Tally clients and one
whose Tally is off.
