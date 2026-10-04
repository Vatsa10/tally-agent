# Demo runbook - a CA firm, 30 minutes, live

The audience runs a practice. They have lived in TallyPrime for years and they
have been sold "Tally integrations" before that turned out to be a file drop.
Every claim made on the agent's screen gets checked on Tally's own screen,
in front of them. Nothing in this demo is a recording.

The order follows their day, not our architecture: a bill comes in, someone
drafts it, a partner decides, it lands in Tally; then the pile of bills; then
the agent working inside Tally itself; then the first of the month.

---

## T-60 minutes: set the desk

```powershell
cd D:\Files\Vatsa\Projects\Deepspeed
uv run python scripts/recover_tally.py          # TallyPrime up, TA-Demo Traders loaded
uv run tallyagent clients check                 # demo: loaded, writable, enabled by R. Mehta
uv run python scripts/demo_prep.py --client demo  # clears stale tickets, restocks Widgets
uv run tallyagent users list                    # R. Mehta (partner), Nikhil (clerk)
uv run tallyagent consent list --client demo    # TA-Demo Traders: enabled by R. Mehta
```

Expected: one client loaded and writable, two users, an empty queue.

Screen layout: TallyPrime on the right half, the terminal on the left half,
font size up two notches (Ctrl + scroll). Close Chrome, WhatsApp, email - the
cursor tier refuses to type when another window takes focus, and you do not
want that refusal to be the thing they remember.

Rehearse the cursor once, off camera, so Tally has drawn its screens:

```powershell
uv run python scripts/tally_ui_rehearse.py screen      # prints Tally's own header text
```

Open the agent for the demo:

```powershell
uv run tallyagent tui --client demo
```

---

## 0:00 - The problem (2 min, no screen)

Ask them, don't tell them: how many purchase bills does an article clerk key in
a month for one client? How many clients? How long does GSTR-2B reconciliation
take on the 14th?

Then: "Everything you are about to see is your Tally, on this laptop, live. The
agent never writes anything to it without a partner's PIN."

Say this early, before anyone reads a trial balance: **the demo company has
deliberate mistakes in it** - cash below zero, the same sale keyed five times,
bills from a supplier who has not filed. Do not apologise for them when they
show up in a report; the month-end chapter finds every one of them.

## 0:02 - Who is at the keyboard (2 min)

```
/users
/signin Nikhil
```

The PIN prompt is masked. Point at the status line: the client, the company,
"writes limited to enabled companies".

> "A clerk drafts. A partner decides. The software knows the difference."

## 0:04 - One sale, in plain words (4 min)

Type:

```
raise a sales invoice to Acme Industries for 2 Widget at 3200 each, 18% GST, dated 2 June 2026
```

Point at the draft: taxable 6,400, **IGST** not CGST+SGST - Acme is in
Maharashtra, the company is in Gujarat. Nobody told it; it read both GSTINs.

```
/approve A
```

Refused: Nikhil is a clerk.

```
/signin R. Mehta
/approvals
/approve <ticket>
```

**Now switch to Tally**: Day Book, 2-Jun-2026 - the voucher is there. Stock
Summary: Widgets down by two.

> "That is your Day Book, not our copy of it."

## 0:10 - What it refuses to do (2 min)

```
book 100 rent to the Baroda branch
```

There is no Baroda cost centre; there is Vadodara, and it says so instead of
guessing. Nothing was queued.

Then the company guard:

```
/client sharma
```

Any write: refused - nobody has enabled Sharma's books. Explain consent:
a partner enables a client once, pinned to Tally's GUID for those books, on the
audit chain with their name.

```
/client demo
```

## 0:12 - The pile of bills (5 min)

```
/bills demo/bills --limit 6 --again
```

While it reads (about a minute): "Each scan is read on this machine. Only the
text goes to the model, never the image." Point at the result table: drafted,
or set aside for a person - an unknown vendor is never invented.

Then the number they will ask about:

```powershell
type reports\bill_accuracy_photo.md
```

Bills photographed the way they arrive - skewed, shadowed, forwarded twice on
WhatsApp: every field right on the pile, the unreadable one refused. Say plainly
it is our pile, and offer to run their own folder of 50 scans through the same
harness before they decide anything.

## 0:17 - It works inside Tally, where they can watch (4 min)

```
/tier3 on
show me the day book in Tally
```

The red ring moves in their Tally. Go To, Day Book, it reads Tally's own header
back before doing anything else. Then a voucher keyed through Tally's screens
(partner approval before Ctrl+A):

```powershell
uv run python scripts/tally_ui_rehearse.py pay --date 2026-06-02 --amount 150.00 --ledger Cash --expense "Bank Charges" --cost-centre Ahmedabad --cost-category Branches
```

> "If a screen is not the one it expected, it stops rather than typing into
> whatever has focus."

## 0:21 - The first of the month (5 min)

```
/monthend 2026-06 tests/fixtures/live_bank_statement.csv tests/fixtures/live_gstr2b.json
```

Walk the findings top down: negative cash, statement lines with no voucher,
**input credit at risk in rupees** because suppliers have not filed, the same
bill entered several times, 90-day receivables. Open the pack:

```powershell
start "reports\close\TA-Demo Traders\2026-06\close.md"
```

Then GSTR-1:

```
/gstr1 2026-06
```

Filing-ready JSON with the problems listed before anything is uploaded.

## 0:26 - The audit chain (2 min)

```powershell
uv run tallyagent audit log --client demo
uv run tallyagent audit verify --client demo
```

Every grant, sign-in, approval and automatic posting, by name, hash-chained.

> "If anything ever goes wrong in a client's books, this answers who allowed it."

## 0:28 - Close (2 min)

What a pilot looks like: their machine, their Tally, two clients they choose,
two weeks. We score their own bills before they trust a batch.

---

## When something goes wrong on the day

| Symptom | Do this |
|---|---|
| "Cannot connect to Tally" | `uv run python scripts/recover_tally.py` - restarts Tally and reloads the company. Keep talking; it takes ~40s. |
| Tally shows the licence / EDU screen | Same command. If it persists, use the Educational mode button and say so - it is a student licence on a demo laptop. |
| Agent answers slowly | It is the model provider. Say so. Reports (`trial balance please`) are instant fallbacks. |
| Cursor refuses: "would not come to the front" | Click the Tally window once, run it again. It refused on purpose. |
| A voucher is refused by validation | That is a feature - read the reason out loud. |
| Bills batch is slow | Use `--limit 3`. |
| Anything else | `trial balance please` and `who owes us money?` always work. |

## Questions they will ask

- **Where does our data go?** Tally data stays on this machine. Only the text
  needed for a question goes to the model, and every byte is logged: `/egress`.
  A local model can replace the cloud one.
- **Can it post without us?** Only what a partner has written into
  `config/policy.toml` as a standing instruction (e.g. receipts under 5,000), and
  even then the audit chain records it as "policy, asked by <name>".
- **What if it gets a GST split wrong?** Validation runs before the queue; the
  partner sees the exact ledger effects to the paisa and the XML before approving.
- **Does it work with our 40 clients?** One register, a database per client;
  `tallyagent clients check` every morning.
- **What does it cost / when?** Pilot first. Price after we have their numbers.
