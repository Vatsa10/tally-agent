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
| 2 | Earned autonomy ramp and no-touch metric | **done** |
| 3 | `reconcile_2b` job: mismatch classification, vendor follow-ups, deferred-ITC ledger, IMS suggestions | **done** |
| 4 | Edit Log edition check, local model option | next |
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

## 2. Earned autonomy - done

`packages/approvals/tallyagent_approvals/autonomy.py` holds the rules; `packages/daemon/tallyagent_daemon/firm/autonomy.py` is the runner's side.

- A partner switches it on per client (`autonomy_grant` table, on the audit
  chain, a partner-only action). The grant sets the streak length (at least 5)
  and an amount ceiling. The system promotes only inside a grant; it never
  grants itself one.
- An action type becomes trusted when its last `min_streak` human decisions
  were all clean approvals. The first rejection, edit or failed post drops it
  back to gated. Posts made by autonomy do not count towards its own record.
- Even when trusted, an action posts only up to the largest amount a person has
  approved in the current streak, and never above the partner's ceiling.
  Validation warnings always go to a person.
- Every autonomous post is signed `autonomy (N clean approvals, granted by
  <partner>)`, so the audit chain says why nobody looked.
- No-touch rate per client: earned posts plus standing-policy posts, over all
  posts. This is the throughput number.

Tests: `tests/test_autonomy.py` (16), including an end-to-end morning run that
posts what earned it and queues the two over-limit receipts, each with its own
reason.

## 3. GSTR-2B reconciliation job - done

Files:
- `packages/tools/tallyagent_tools/itc.py`: matching and causes.
- `packages/approvals/tallyagent_approvals/deferred_itc.py`: the deferred-ITC ledger.
- `packages/daemon/tallyagent_daemon/firm/reconcile.py`: the job.

What it does:
- **Reads the portal's real GSTR-2B shape**: `dt`, `igst`/`cgst`/`sgst`/`cess`, `itcavl`/`rsn`. It also reads the GSTR-1 style.
- **Matches on supplier GSTIN plus invoice number.** Invoice numbers are normalised for case, punctuation and leading zeros. If the GSTIN doesn't match, it falls back to the supplier name, which is how a wrong GSTIN in the books gets caught.
- **Causes, each with ITC at risk (the tax) and an action:** matched, supplier has not filed, bill not entered, ITC not available (per 2B), taxable value differs, tax differs (rate), and GSTIN mismatch.
- **Unmatched bills from other months** are left to their own month's run and the deferred ledger, so the same credit is never counted twice.
- **Deferred-ITC ledger**:
  - Unfiled invoices are carried month to month. A re-run doesn't add them twice.
  - When an invoice appears in a later 2B it is called claimable.
  - Invoices within 60 days of the section 16(4) deadline are flagged.
- **Follow-ups**: one ready-to-send message per supplier who has not filed, listing invoice, date and tax.
- **IMS suggestions**: accept or pending for each 2B invoice, with a reason. It never touches the portal.
- **Drafts purchase vouchers** for bills that are in 2B but not in the books, where the supplier is already a ledger. The rate comes from the 2B tax. Drafts go to approval, or post under earned autonomy. Unknown suppliers are never invented.
- **Missing 2B download**: says exactly where to put the file, with the 3B due date.

Bugs found and fixed while building it:
1. **The old matcher keyed on the invoice number alone**, so two suppliers' bill "12" collided.
2. **It read only GSTR-1 style tax keys**, so a real 2B download read every tax as zero.
3. **The close pack's "ITC at risk" summed invoice totals instead of tax.** That overstated it about 6x on the demo books (2,82,610 against a true 43,110).
4. **`/reco gstr2b` without dates saw no books**, because the default window was today.
5. **The live 2B fixture was labelled May but carried June invoices.**

Live, against TA-Demo Traders: `/reco gstr2b` and `/monthend` agree on Rs 43,110 at risk; one value difference and one bill not entered are found; and an April bill the supplier reported late is matched.

Tests: `tests/test_itc.py` (19), `tests/test_reconcile_job.py` (10), `tests/test_close.py` (+1).
