"""Version-history tests (git-backed backups replacing the old .bak scheme).

Runs against a COPY of the collection (never the original), with an isolated
throwaway app-data dir. Verifies:
  * opening a collection records a baseline commit
  * a save adds a commit with a non-empty edit summary
  * a byte-identical save is deduped (no new commit)
  * list_history is newest-first
  * restoring an earlier version round-trips the .nml bytes AND adds a
    "Restored …" commit (a forward save, not a rewind)
  * clear_history removes the repo
  * no .bak files are left next to the collection

Run: `python test_history.py` in the backend venv (see run_tests.sh).
"""
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
work_dir = Path(tempfile.mkdtemp(prefix="konduktor-history-"))
WORK = work_dir / "collection.nml"
shutil.copy2(REAL, WORK)

os.environ["KONDUKTOR_NML"] = str(WORK)
os.environ["KONDUKTOR_DATA_DIR"] = str(work_dir / "appdata")

from fastapi.testclient import TestClient  # noqa: E402

from konduktor import history, main  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


client = TestClient(main.app)


def first_playlist(nodes):
    for n in nodes:
        if n["type"] == "PLAYLIST":
            return n
        if n.get("children"):
            found = first_playlist(n["children"])
            if found:
                return found
    return None


print("== baseline on open ==")
hist = client.get("/api/history").json()
check("baseline commit exists", len(hist) == 1)
check("baseline summary set", hist and "Baseline" in hist[0]["summary"])

original_bytes = WORK.read_bytes()

print("== save records a commit ==")
pl = first_playlist(client.get("/api/playlists").json())
new_name = pl["name"] + " ✎"
r = client.patch(f"/api/playlists/{pl['id']}", json={"name": new_name})
check("rename ok", r.status_code == 200)
res = client.post("/api/save").json()
check("save reports a commit", bool(res["commit"]))
hist = client.get("/api/history").json()
check("history grew to 2", len(hist) == 2)
check("newest is the rename", hist and new_name in hist[0]["summary"])
check("newest-first ordering", "Baseline" in hist[-1]["summary"])

print("== dedup: byte-identical save makes no commit ==")
# Force a save with no real change: rename back and forth nets a re-render of the
# same content is not guaranteed; instead call history.commit directly with HEAD.
head_bytes = WORK.read_bytes()
before = len(client.get("/api/history").json())
dup = history.commit(WORK, head_bytes, "should be deduped", "test")
after = len(client.get("/api/history").json())
check("dedup returns None", dup is None)
check("dedup adds no commit", after == before)

print("== restore round-trips bytes + adds a forward commit ==")
hist = client.get("/api/history").json()
baseline_id = hist[-1]["id"]
count_before = len(hist)
rr = client.post(f"/api/history/{baseline_id}/restore")
check("restore ok", rr.status_code == 200)
check("restored bytes == original", WORK.read_bytes() == original_bytes)
hist = client.get("/api/history").json()
check("restore added a commit", len(hist) == count_before + 1)
check("restore commit labelled", hist and "Restored" in hist[0]["summary"])

print("== read_version returns the right blob ==")
check("baseline blob == original", history.read_version(WORK, baseline_id) == original_bytes)

print("== no .bak litter ==")
check("no .bak files next to collection", not list(work_dir.glob("*.bak")))
check("no backups/ dir next to collection", not (work_dir / "backups").exists())

print("== clear_history removes the repo ==")
cr = client.delete("/api/history")
check("clear ok", cr.status_code == 200)
check("history empty after clear", client.get("/api/history").json() == [])

sys.exit(1 if failed else 0)
