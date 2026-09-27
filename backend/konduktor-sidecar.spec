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
#  - dulwich (version-history backups) imports git backends dynamically →
#    collect_submodules('dulwich')
from PyInstaller.utils.hooks import collect_submodules, collect_all

hiddenimports = []
datas = []
binaries = []

# Dynamically-imported subpackages.
for pkg in ("uvicorn", "xsdata", "dulwich"):
    hiddenimports += collect_submodules(pkg)

# Native standard extras — grab modules + shared libs + any data. PyAV (`av`)
# carries FFmpeg's shared libraries, which the export's waveform analysis needs.
for pkg in ("uvloop", "httptools", "websockets", "av"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Imported by name, not statically discoverable.
hiddenimports += ["multipart", "anyio"]

# Bundle the canonical version file so konduktor.__version__ can read it at
# runtime from sys._MEIPASS (frontend/package.json is the single source of the
# app version — see konduktor/__init__.py). Path is relative to this spec.
datas += [("../frontend/package.json", ".")]

# The Rekordbox and OneLibrary exporters build their databases from the real
# DDL + scaffolding rows under fixtures/, found at `parents[3] / "fixtures"` —
# i.e. the bundle root. Without these a packaged app cannot export to either.
datas += [
    ("fixtures/rekordbox/schema.sql", "fixtures/rekordbox"),
    ("fixtures/rekordbox/seed.sql", "fixtures/rekordbox"),
    ("fixtures/onelibrary/schema.sql", "fixtures/onelibrary"),
    ("fixtures/onelibrary/seed.sql", "fixtures/onelibrary"),
    # The "Rekordbox Export" target starts from rekordbox's own empty device library.
    ("fixtures/rekordbox/device/export.pdb", "fixtures/rekordbox/device"),
    ("fixtures/rekordbox/device/exportExt.pdb", "fixtures/rekordbox/device"),
]

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
