# Running tallyagent against a live TallyPrime

Everything up to Stage 13 runs against an in-process fake. This is what changes
when you point it at a real installation.

Verified against **TallyPrime 1.1.7.1, Educational (student) mode**, Windows.

---

## 1. What the agent does for you

You need TallyPrime installed and running. That is all.

```bash
uv run tallyagent enable-server          # turns on the XML interface, restarts Tally
uv run tallyagent probe                  # confirms it
```

`enable-server` locates Tally (running process, then the usual directories, then
the uninstall registry keys), backs up `tally.ini`, sets three keys, restarts
Tally, and waits for the port:

```ini
Client Server=Both
ServerPort=9000
Enable ODBC Server=Yes
```

Those key names were read off a real install, not guessed — see
[TALLY_INI_KEYS.md](TALLY_INI_KEYS.md). If a future version renames them, the
tool falls back to driving Tally's Connectivity screen with keystrokes and then
diffs `tally.ini` to learn the new names.

### Doing it by hand instead

F1 (Help) → Settings → Connectivity → Client/Server configuration:
*TallyPrime acts as* = **Both**, *Port* = **9000**, *Enable ODBC* = **Yes**.
Accept, restart Tally.

Either way, this works when it is on:

```bash
curl http://127.0.0.1:9000
# <RESPONSE>TallyPrime Server is Running</RESPONSE>
```

The window title also becomes `TallyPrime:9000`, which is a quicker check.

### If Windows Firewall prompts

Answer it yourself. The agent will not click OS dialogs — a tool that clicks
through security prompts is a tool that clicks through the wrong one eventually.
Binding to `127.0.0.1` usually avoids the prompt entirely.

---

## 2. Educational mode

Two constraints, both handled, both worth knowing:

**Vouchers may only be dated the 1st, 2nd or 31st of a month.** Enforced as the
`edu_date_allowed` validation rule, so an illegal date is a readable report line
before anything is sent, not an opaque Tally error afterwards. `snap_to_edu_date`
moves a date to the nearest legal one and *always* warns — a date moved silently
is a voucher in the wrong period, and at a month boundary the wrong GST return.

It defaults on in live mode. On a licensed install:

```toml
[tally]
edu = false
```

**The licence screen.** A fresh EDU install may open on
*Configure Existing License* (port 9999 — the licence gateway, unrelated to the
XML port). Press Esc to reach the Gateway, then choose
**T: Continue In Educational Mode**.

---

## 3. Live mode and the write scope

```toml
mode = "live"

[tally]
host = "127.0.0.1"
port = 9000
company = "TA-Demo Traders"
write_prefix = "TA-"
```

In live mode tallyagent will only write to companies whose name starts with
`write_prefix`. A write aimed anywhere else is refused **before** validation,
before the approval queue, before any XML is built. This is what protects a
firm's real books if they appear on the same machine later.

An empty prefix allows nothing rather than everything: the misconfiguration that
would otherwise mean "write anywhere" is the one that must fail closed.

`--fake-tally` forces fake mode, because the guard is about real books.

---

## 4. Creating a company

```bash
uv run tallyagent tui
```

then `/bootstrap TA-Demo Traders`.

**On TallyPrime 1.1.7.1 this uses the keyboard, not the API.** Every XML
`Import Data` request resolves a current company first, so with none loaded a
company-create import returns:

```xml
<LINEERROR>Could not find Company &apos;&apos;</LINEERROR>
```

`bootstrap_company` tries XML anyway (cheap, harmless, and a later Tally may
support it), then falls back to driving the Create Company screen. Every field
change is a mutating Tier 3 step and asks you first, showing the screen as a
braille preview. Which path was used is recorded in the audit log.

Afterwards the company is verified over XML before the step is called done.

### Doing it by hand instead

Select Company → **Create Company** → name, State, Financial year from →
Ctrl+A. On the features screen leave *Maintain Accounts*, *Enable Bill-wise
entry* and *Enable GST* as **Yes** — the tools rely on all three.

---

## 5. Filling it in

From the TUI:

```
/seed coa          # chart of accounts + demo parties, ONE approval, one diff
/seed txns 4       # sales, purchase, receipt, payment - each queued for you
/approvals         # what is waiting
```

Press `a` to approve the highlighted item, `x` to see the exact XML, `v` for the
validation report, `r` to reject with a reason.

---

## 6. The full end-to-end run

```bash
uv run python scripts/live_e2e.py                 # you approve each write
uv run python scripts/live_e2e.py --auto-approve  # unattended
```

It runs `scenarios/e2e_edu_bootstrap.yaml` — the same file CI runs against the
fake (`uv run pytest tests/e2e -q`) — and writes
`reports/live_e2e_<timestamp>.md` with every step and its output.

It refuses to start if a company outside the write scope is loaded.

---

## 7. What to check in Tally afterwards

| Where | What you should see |
|---|---|
| Gateway of Tally | Company **TA-Demo Traders**, period 1-Apr-26 to 31-Mar-27, a *Date of last entry* |
| Gateway → Chart of Accounts → Ledgers | Sales/Purchase - GST 18%, Output & Input CGST/SGST/IGST, Round Off, Bank, and the two parties |
| Gateway → Day Book (F2 for the date range) | The sales, purchase, receipt and payment vouchers |
| Gateway → Balance Sheet | Balanced |
| A sales voucher | IGST for an inter-state party, CGST+SGST for one in your own state |

Cross-check against what tallyagent believes:

```bash
uv run tallyagent probe
uv run tallyagent audit verify
uv run tallyagent approvals list --status approved
```

---

## 8. Things live Tally does that the fake taught us to expect

Each of these cost a real debugging session; they are listed so the next one is
cheaper. All are handled in the adapter and reproduced by the fake.

| Behaviour | Consequence |
|---|---|
| The response charset mirrors the request's | Declaring `charset=utf-16` while sending UTF-8 gets `Unknown Request, cannot be processed` at HTTP 200 on *every* call. We send and declare UTF-8. |
| `List of Ledgers` returns **names only** | No parent, no GSTIN, no balance. Fields need an explicit TDL `<FETCH>`. |
| A TDL collection without a description **crashes Tally** | A crash dump, and the process gone. Only the `<TYPE>` + `<FETCH>` form is used. |
| No version over XML | The banner is just `TallyPrime Server is Running`. Version comes from the `tally.exe` file version. |
| Ledgers carry no usable state field | A party's GST state comes from the first two digits of its GSTIN. |
| Import needs a current company | Even to create one. Hence the keyboard path. |
| Control characters in responses | `&#4;` appears inside real group names; the parser strips them. |

---

## 9. Recovering

**Tally crashed.** Your data is on disk (`C:\Users\Public\TallyPrime\data`).
Start it again; `enable-server` will too. `tally1.dmp` and `tallyerr.log` in the
install directory say what happened (both UTF-16).

**The port stopped answering.** Check the window title says `TallyPrime:9000`,
then `uv run tallyagent probe` — it names the specific problem and what to run.

**A voucher will not post.** `uv run tallyagent approvals list` shows it with its
validation report. Tally's own `<LINEERROR>` is in the audit log:
`uv run tallyagent audit log --limit 20`.

**Start over.** Delete the company folder under the data directory, or create a
differently-named `TA-` company. tallyagent will not touch anything outside that
prefix.
