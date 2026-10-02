"""Layering guard: `core` must not know any platform exists.

The whole value of the adapter split is that `konduktor.core` can be read as the
contract a future Rekordbox or Serato adapter is written against. One stray
import of a native type would quietly make it Traktor-shaped again, and nothing
else in the suite would notice.
"""
import ast
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent / "konduktor" / "core"
FORBIDDEN = ("traktor_nml_utils", "konduktor.adapters", "xsdata")

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


print("== core/ imports nothing platform-specific ==")
offences: list[str] = []
for src in sorted(CORE.rglob("*.py")):
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; ".." from core/ reaches konduktor,
            # which is allowed (paths, prefs), but "...adapters" is not.
            names = [node.module or ""]
            if node.level and "adapters" in (node.module or ""):
                offences.append(f"{src.name}: relative import of {node.module}")
        for n in names:
            if any(n == f or n.startswith(f + ".") for f in FORBIDDEN):
                offences.append(f"{src.name}: {n}")

check("no core module imports a native library type", not offences, "; ".join(offences))

print("== adapters own their native model ==")
store = (CORE.parent / "adapters" / "traktor" / "store.py").read_text()
check("the Traktor store is where traktor_nml_utils lives", "traktor_nml_utils" in store)

# No adapter may import another platform's library. That is the rule that keeps
# the packages independently removable, and it is checked on real imports (AST)
# rather than on text, so merely NAMING another platform in a comment is fine.
# OneLibrary shares Rekordbox's native library: both are AlphaTheta formats
# read through pyrekordbox. That makes the map non-injective, which is fine —
# the rule being enforced is "no adapter reaches for a RIVAL vendor's library",
# and a shared dependency is not a layering breach. (The OneLibrary adapter
# also imports the Rekordbox adapter's ANLZ beatgrid helper on purpose: one
# definition of "when does a tempo change start a new marker" is worth more
# than two packages that can be deleted independently.)
NATIVE = {
    "traktor": "traktor_nml_utils",
    "rekordbox": "pyrekordbox",
    "onelibrary": "pyrekordbox",
}


def imported_modules(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            names.add(node.module or "")
    return names


for platform, module in NATIVE.items():
    pkg = CORE.parent / "adapters" / platform
    if not pkg.is_dir():
        continue
    # Subtract the adapter's OWN module: two adapters may legitimately share a
    # native library (Rekordbox and OneLibrary both use pyrekordbox), and
    # without this every shared dependency reads as a foreign one.
    foreign = {m for p_, m in NATIVE.items() if p_ != platform} - {module}
    strays = []
    for src in sorted(pkg.rglob("*.py")):
        for name in imported_modules(src):
            if any(name == f or name.startswith(f + ".") for f in foreign):
                strays.append(f"{src.name}: {name}")
    check(f"the {platform} adapter imports no other platform's library",
          not strays, "; ".join(strays))
    own = [
        src.name
        for src in sorted(pkg.rglob("*.py"))
        if any(n == module or n.startswith(module + ".") for n in imported_modules(src))
    ]
    check(f"the {platform} adapter does import {module} somewhere", bool(own), "nowhere")

print("== remote libraries ==")
# The remote adapter speaks a PROTOCOL; which platform the server holds is the
# server's business. Importing a platform's adapter (or its library) here would
# make a remote library quietly platform-shaped again.
remote_pkg = CORE.parent / "adapters" / "remote"
strays = []
for src in sorted(remote_pkg.rglob("*.py")):
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level and any(mod == p or mod.startswith(p + ".") for p in NATIVE):
                strays.append(f"{src.name}: ..{mod}")
            if not node.level and any(mod.startswith(m) for m in set(NATIVE.values())):
                strays.append(f"{src.name}: {mod}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if any(a.name.startswith(m) for m in set(NATIVE.values())):
                    strays.append(f"{src.name}: {a.name}")
check("the remote adapter imports no platform adapter or library", not strays, "; ".join(strays))

# The server is lightweight by decision: it never decodes audio, so importing
# it must not pull in the analysis stack (and its image ships without it).
import subprocess

probe = subprocess.run(
    [sys.executable, "-c",
     "import sys, konduktor.server.app; "
     "print(','.join(m for m in ('librosa', 'sklearn', 'torch', 'numba', 'av') if m in sys.modules))"],
    capture_output=True, text=True, cwd=str(CORE.parents[1]),
)
heavy = probe.stdout.strip()
check("the server imports no audio-analysis library", probe.returncode == 0 and not heavy,
      heavy or probe.stderr[-300:])

print("\nRESULT:", "FAILED" if failed else "ALL PASSED")
sys.exit(1 if failed else 0)
