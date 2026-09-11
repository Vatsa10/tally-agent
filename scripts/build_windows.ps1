# Build a single-file Windows executable for the tallyagent daemon.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
#
# Output: dist\tallyagent.exe
# The exe still reads config\config.toml and config\policy.toml from the working
# directory - it is a daemon, not an installer, and the config is the operator's.

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "==> Syncing dependencies (dev + desktop + mcp)"
uv sync --extra dev --extra desktop --extra mcp

Write-Host "==> Running the test suite; a failing build must not ship"
uv run pytest -q
if ($LASTEXITCODE -ne 0) { throw "tests failed; not building" }

Write-Host "==> Building dist\tallyagent.exe"
uv run pyinstaller --noconfirm --clean tallyagent.spec

$exe = "dist\tallyagent.exe"
if (-not (Test-Path $exe)) { throw "expected $exe to exist" }

Write-Host "==> Smoke test against the fake Tally"
& $exe probe --fake-tally
if ($LASTEXITCODE -ne 0) { throw "the built exe could not probe the fake Tally" }

Write-Host ""
Write-Host "Built $exe"
Write-Host "Next: copy config\config.example.toml to config\config.toml, set"
Write-Host "[tally] host = `"127.0.0.1`", then run: .\dist\tallyagent.exe serve"
