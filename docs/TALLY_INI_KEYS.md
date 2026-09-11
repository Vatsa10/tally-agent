# tally.ini connectivity keys

Read from a real installation, not guessed. Verified against:

| | |
|---|---|
| Product | TallyPrime |
| Version | 1.1.7.1 |
| Install dir | `C:\Program Files\TallyPrime` |
| Data dir | `C:\Users\Public\TallyPrime\data` |
| Read on | 2026-09-11 |

`tally.ini` lives in the install directory next to `tally.exe`.

## The keys that matter

| Key | Values | Meaning |
|---|---|---|
| `Client Server` | `Both` / `Server` / `Client` / `None` | Whether Tally listens. `Both` and `Server` open the XML port. |
| `ServerPort` | integer | The XML-over-HTTP port. `9000` conventionally. |
| `Enable ODBC Server` | `Yes` / `No` | The ODBC interface. TallyPrime's connectivity screen sets it alongside server mode. |
| `Data` | path | Where companies live on disk. Useful for confirming a company was created. |

A working configuration reads:

```ini
Client Server=Both
ServerPort=9000
Enable ODBC Server=Yes
Data=C:\Users\Public\TallyPrime\data
```

## Format notes

`tally.ini` is *not* a standard INI file and `configparser` mangles it:

- comments use `;;`, and commented examples sit directly above live keys;
- keys contain spaces (`Client Server`, `Enable ODBC Server`);
- values contain `:` and `\` (`Tally Gateway Server=localhost:9999`, Windows paths);
- there is a single `[TALLY]` section header and keys appear after it in no
  particular order.

`tallyagent_tally.install.read_connectivity` parses it line by line for this
reason, and `set_connectivity` rewrites only the lines it must, preserving the
rest of the file byte for byte. Every write takes a timestamped backup first
(`tally.ini.tallyagent-<stamp>.bak`).

## Confirming without reading the file

Once server mode is on, Tally puts the port in its window title:

```
TallyPrime:9000
```

`install.configured_port_from_title()` reads that. It is a useful cross-check:
`tally.ini` says what Tally will do *next start*, the title says what the
running process is actually doing.

## If these keys are ever wrong

`enable_tally_server` falls back to driving the Connectivity screen with
keystrokes (Tier 3), and then diffs `tally.ini` before and after to learn the
real key names for that version. Update this file from that diff rather than
guessing — a wrong key name silently does nothing, which is the worst failure
mode available here.
