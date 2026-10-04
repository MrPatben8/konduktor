"""userprefs.json under concurrent writes.

The UI sends several `PATCH /api/prefs` at once on launch (column layout, deck
zoom, loop size), and FastAPI runs them on a thread pool. Before the lock and
the atomic write, a reader could catch the file truncated mid-write, take it for
"no prefs" and write back only its own key — the column layout, and with it
every other pref, vanished between sessions (249 of 300 trials of the burst
below lost the other keys). Nothing failed; the app just forgot.

Prefs are redirected to a temp file — a test must never rewrite the user's own.
"""
import tempfile
import threading
from pathlib import Path

from konduktor import prefs

_TMP = tempfile.TemporaryDirectory()
prefs._PREFS_PATH = Path(_TMP.name) / "userprefs.json"
prefs._LEGACY_PREFS_PATH = Path(_TMP.name) / "absent.json"

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


BASE = {"columns": {"order": ["a", "b"]}, "autoCueTemplate": [1, 2, 3], "beatSize": 4}
PATCHES = [
    {"columns": {"order": ["b", "a"]}},
    {"mainZoomSec": 16},
    {"beatSize": 8},
    {"stemDevice": "cpu"},
]

# The launch burst, many times over: every patch lands and nothing else is lost.
TRIALS = 200
bad = []
for i in range(TRIALS):
    prefs.save_prefs(BASE)
    go = threading.Barrier(len(PATCHES) + 1)

    def write(p):
        go.wait()
        prefs.update_prefs(p)

    def read():
        go.wait()
        for _ in range(20):
            if "autoCueTemplate" not in prefs.load_prefs():
                bad.append(f"trial {i}: a reader saw no prefs")
                return

    threads = [threading.Thread(target=write, args=(p,)) for p in PATCHES]
    threads.append(threading.Thread(target=read))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = prefs.load_prefs()
    want = {**BASE, **{k: v for p in PATCHES for k, v in p.items()}}
    if got != want:
        bad.append(f"trial {i}: {got}")
check(f"{TRIALS} concurrent bursts keep every key and every patch", not bad,
      bad[0] if bad else "")

# The path-specific helpers share the lock: a burst of them loses nothing either.
prefs.save_prefs({"autoCueTemplate": [1]})
threads = [threading.Thread(target=prefs.set_last_collection, args=(f"/lib/{p}", p))
           for p in ("traktor", "rekordbox", "onelibrary")]
threads += [threading.Thread(target=prefs.set_path_mapping, args=(f"/c{n}", "X:", "/Volumes/X"))
            for n in range(3)]
for t in threads:
    t.start()
for t in threads:
    t.join()
got = prefs.load_prefs()
check("concurrent last-collection writes keep every platform",
      set(got.get("last_collection_by_platform", {})) == {"traktor", "rekordbox", "onelibrary"},
      str(got))
check("concurrent path mappings keep every collection",
      set(got.get("path_mappings", {})) == {"/c0", "/c1", "/c2"}, str(got))
check("and the unrelated key survives", got.get("autoCueTemplate") == [1])

# A file that will not parse is moved aside, never silently overwritten.
prefs._PREFS_PATH.write_text('{"columns": {"order": ["a"')
check("an unreadable file reads as no prefs", prefs.load_prefs() == {})
check("reading leaves it in place", prefs._PREFS_PATH.read_text().startswith('{"columns"'))
prefs.update_prefs({"beatSize": 2})
corrupt = prefs._PREFS_PATH.with_name("userprefs.json.corrupt")
check("writing over it keeps the old bytes beside it",
      corrupt.exists() and corrupt.read_text() == '{"columns": {"order": ["a"')
check("and the new file holds the patch", prefs.load_prefs() == {"beatSize": 2})

check("no temp file is left behind",
      not [p for p in Path(_TMP.name).iterdir() if ".tmp" in p.name])

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
_TMP.cleanup()
raise SystemExit(1 if failed else 0)
