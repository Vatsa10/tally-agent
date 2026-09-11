# PyInstaller spec: one Windows .exe for the tallyagent daemon.
#   uv run pyinstaller tallyagent.spec
# Produces dist/tallyagent.exe. See scripts/build_windows.ps1.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
PACKAGES = ROOT / "packages"

# The Jinja templates are read from disk at runtime, so they have to travel with
# the binary; PyInstaller cannot see them from the imports alone.
datas = [
    (
        str(PACKAGES / "channels/tallyagent_channels/web/templates"),
        "tallyagent_channels/web/templates",
    ),
]

# uvicorn and sqlmodel both load pieces of themselves by name at runtime.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("sqlmodel")
    + ["tallyagent_mcp_server.server"]
)

a = Analysis(
    [str(PACKAGES / "daemon/tallyagent_daemon/cli.py")],
    pathex=[str(p) for p in PACKAGES.iterdir() if p.is_dir()],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tallyagent",
    console=True,
    debug=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
)
