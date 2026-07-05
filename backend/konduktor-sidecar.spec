# PyInstaller spec for the Konduktor backend sidecar (Step 1 of the standalone
# build). Produces a single-file binary that serves the FastAPI API on a free
# loopback port. Build with:  pyinstaller konduktor-sidecar.spec  (or build_sidecar.sh)
#
# The tricky bits this handles:
#  - uvicorn imports its loop/protocol backends dynamically → collect_submodules
#  - the standard extras (uvloop/httptools/websockets) are native → collect_all
#  - traktor-nml-utils parses/serializes via xsdata, which discovers plugins at
#    runtime → collect_submodules('xsdata')
#  - python-multipart is imported as `multipart` by starlette → hidden import
from PyInstaller.utils.hooks import collect_submodules, collect_all

hiddenimports = []
datas = []
binaries = []

# Dynamically-imported subpackages.
for pkg in ("uvicorn", "xsdata"):
    hiddenimports += collect_submodules(pkg)

# Native standard extras — grab modules + shared libs + any data.
for pkg in ("uvloop", "httptools", "websockets"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Imported by name, not statically discoverable.
hiddenimports += ["multipart", "anyio"]

a = Analysis(
    ["sidecar.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # watchfiles is dev-reload only; the frozen sidecar never uses --reload.
    excludes=["watchfiles"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="konduktor-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep stdout so the host can read KONDUKTOR_PORT
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
