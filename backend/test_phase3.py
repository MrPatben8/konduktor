"""End-to-end write test for Phase 3.

Runs against a COPY of the collection (never the original). Verifies:
  * create / add-tracks / reorder / rename / delete all work
  * save creates a backup and produces a Traktor-parseable file
  * the COLLECTION section is byte-identical after save (truly surgical)
  * changes survive a reload
"""
import os
import re
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

REAL = Path(__file__).resolve().parents[1] / "collection.nml"
work_dir = Path(tempfile.mkdtemp(prefix="konduktor-test-"))
WORK = work_dir / "collection.nml"
shutil.copy2(REAL, WORK)

# Snapshot the real file up front; the test only ever touches the copy, so the
# real file must be untouched at the end (regardless of its absolute size).
REAL_STAT = (REAL.stat().st_size, REAL.stat().st_mtime)

os.environ["KONDUKTOR_NML"] = str(WORK)

from fastapi.testclient import TestClient  # noqa: E402
from traktor_nml_utils import TraktorCollection  # noqa: E402
import konduktor.main as m  # noqa: E402

client = TestClient(m.app)


def collection_span(data: bytes) -> bytes:
    s = re.search(rb"<COLLECTION[ >]", data).start()
    e = re.search(rb"</COLLECTION>", data).end()
    return data[s:e]


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        check.failed = True


check.failed = False

print("== initial read ==")
pls = client.get("/api/playlists").json()
flat = lambda ns: [x for n in ns for x in [n] + flat(n.get("children", []))]
playlists = [n for n in flat(pls) if n["type"] == "PLAYLIST"]
print(f"  {len(playlists)} playlists, e.g. {[p['name'] for p in playlists[:3]]}")
donor = next(p for p in playlists if p["count"] >= 5)
donor_tracks = client.get(f"/api/playlists/{donor['id']}/tracks").json()
track_ids = [t["id"] for t in donor_tracks[:5]]
print(f"  donor '{donor['name']}' -> using {len(track_ids)} track ids")

collection_before = collection_span(WORK.read_bytes())

print("== create + add + reorder + rename ==")
created = client.post("/api/playlists", json={"name": "Konduktor Test"}).json()
new_uuid = created["uuid"]
check("create returns uuid", bool(new_uuid))

r = client.put(f"/api/playlists/{new_uuid}/entries", json={"track_ids": track_ids})
check("add 5 entries", r.json().get("count") == 5)

reordered = list(reversed(track_ids))
client.put(f"/api/playlists/{new_uuid}/entries", json={"track_ids": reordered})

client.patch(f"/api/playlists/{new_uuid}", json={"name": "Konduktor Test 2"})

st = client.get("/api/state").json()
check("state is dirty before save", st["dirty"] is True)

print("== save ==")
res = client.post("/api/save").json()
check("saved", res["saved"] is True)
backup = Path(res["backup"])
check("backup file exists", backup.exists())
check("backup matches original size", backup.stat().st_size == REAL.stat().st_size)
st = client.get("/api/state").json()
check("state clean after save", st["dirty"] is False)

print("== verify saved file ==")
data_after = WORK.read_bytes()
check(
    "COLLECTION section byte-identical (surgical write)",
    collection_span(data_after) == collection_before,
)
# Re-parse with the real Traktor library (proves it stays v4-valid).
coll = TraktorCollection(path=WORK)
check("file re-parses with traktor-nml-utils", len(coll.nml.collection.entry) == 8485)


def walk(n):
    yield n
    if n.subnodes:
        for s in n.subnodes.node:
            yield from walk(s)


found = [
    n
    for n in walk(coll.nml.playlists.node)
    if n.type == "PLAYLIST" and n.playlist and n.playlist.uuid == new_uuid
]
check("new playlist present in saved file", len(found) == 1)
if found:
    node = found[0]
    check("renamed correctly", node.name == "Konduktor Test 2")
    check("ENTRIES attr synced to 5", node.playlist.entries == 5)
    saved_keys = [e.primarykey.key for e in node.playlist.entry]
    check("entries reordered correctly", saved_keys == reordered)

print("== reload + delete ==")
client.post("/api/reload")
pls2 = flat(client.get("/api/playlists").json())
check("new playlist visible after reload", any(n["id"] == new_uuid for n in pls2))
client.delete(f"/api/playlists/{new_uuid}")
res2 = client.post("/api/save").json()
coll2 = TraktorCollection(path=WORK)
still = [
    n for n in walk(coll2.nml.playlists.node)
    if n.type == "PLAYLIST" and n.playlist and n.playlist.uuid == new_uuid
]
check("playlist gone after delete+save", len(still) == 0)
check("COLLECTION still byte-identical", collection_span(WORK.read_bytes()) == collection_before)

print("== original untouched ==")
check(
    "original collection.nml never modified",
    (REAL.stat().st_size, REAL.stat().st_mtime) == REAL_STAT,
)

shutil.rmtree(work_dir, ignore_errors=True)
print("\nRESULT:", "FAILED" if check.failed else "ALL PASSED")
sys.exit(1 if check.failed else 0)
