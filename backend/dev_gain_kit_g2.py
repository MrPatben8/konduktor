"""Kit G2 — what rekordbox 7 does with auto gain, and whether a stick carries it
(dev only; see .claude/discussions/discuss-gain-normalization-2026-10-19.md).

Already measured without rekordbox open (2026-10-19, 19 local tracks):
`djmdMixerParam.Gain{High,Low}` are the halves of a big-endian float32, LINEAR,
= 10^((-10 - LUFS)/20) to 0.13 dB — rekordbox levels integrated BS.1770
loudness to -10 LUFS. `Peak{High,Low}` = the linear SAMPLE peak.

Every SONG in the local library was added by Konduktor (2026-10-01), whose
rekordbox add writes no MixerParam row — so the fit above is rekordbox's
sample pack only. This kit asks rekordbox itself:
  A. A track with NO row: does loading it create one (R2, R3)?
  B. After rekordbox analyses the songs: is the gain -10 - LUFS on music too?
  C. Is a gain Konduktor writes KEPT (R1 at -20 dB)?
  D. Does a USB export carry the gain anywhere a CDJ reads (OneLibrary
     exportLibrary.db, export.pdb, the ANLZ files)? R1-R3 are exported twice
     with different gains and the two exports diffed.

    python dev_gain_kit_g2.py prepare     # records state; writes nothing
    #  A -> rekordbox: Auto Gain on, load R2 + R3, quit
    python dev_gain_kit_g2.py status
    #  B -> rekordbox: Analyze the 7 songs, quit
    python dev_gain_kit_g2.py status
    python dev_gain_kit_g2.py write       # R1 -> -20 dB
    #  C -> rekordbox: load R1, export R1-R3 to a stick, quit
    python dev_gain_kit_g2.py check1 /Volumes/STICK
    python dev_gain_kit_g2.py flip
    #  D -> rekordbox: remove R1-R3 from the stick, export again, quit
    python dev_gain_kit_g2.py check2 /Volumes/STICK
    python dev_gain_kit_g2.py restore     # R1-R3 back to rekordbox's own gains

rekordbox must be closed whenever this script runs. The local master.db is written directly (it is disposable — but `prepare`
records every row it touches and `restore` puts them back).
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import struct
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KIT = Path.home() / "Music/Konduktor Stem Test/Kit G2"
STATE = KIT / "state.json"

FLIP = {"R1": -8.0, "R2": +6.0, "R3": -14.0}          # export 2's gains


def _halves(x: float) -> tuple[int, int]:
    bits = struct.unpack(">I", struct.pack(">f", x))[0]
    return bits >> 16, bits & 0xFFFF


def _value(hi, lo) -> float | None:
    if hi is None or lo is None:
        return None
    return struct.unpack(">f", struct.pack(">I", ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)))[0]


def _db_str(lin: float | None) -> str:
    return "—" if lin is None else ("-inf" if lin <= 0 else f"{20 * math.log10(lin):+.2f} dB")


def _db():
    from pyrekordbox import masterdb

    from konduktor.adapters.rekordbox.store import rekordbox_probe_suppressed
    from pyrekordbox.masterdb import database as rb_database

    if rb_database.get_rekordbox_pid():
        raise SystemExit("Quit rekordbox first.")
    return masterdb.MasterDatabase(), rekordbox_probe_suppressed


def _row(db, content_id):
    from pyrekordbox.masterdb import models

    return db.query(models.DjmdMixerParam).filter_by(ContentID=content_id).one_or_none()


def _snapshot_row(r) -> dict | None:
    if r is None:
        return None
    return {c: getattr(r, c) for c in ("ID", "ContentID", "GainHigh", "GainLow", "PeakHigh", "PeakLow", "UUID")} | {
        "created_at": str(r.created_at), "updated_at": str(r.updated_at)}


def _set_gain(db, content_id: str, db_gain: float) -> None:
    r = _row(db, content_id)
    r.GainHigh, r.GainLow = _halves(10 ** (db_gain / 20))


SONGS = {"R1": "Faith in You - AC Slater Remix", "R2": "Low Frequency", "R3": "Never Let You Go"}  # unlocked: a locked track cannot be analysed


def prepare() -> None:
    """Record the kit's songs, their LUFS and their MixerParam rows. Writes nothing:
    every song in this library was added by Konduktor and has NO MixerParam row."""
    from konduktor.core import loudness

    db, _ = _db()
    state = {"tracks": {}, "songs": {}, "created": datetime.now().isoformat()}
    KIT.mkdir(parents=True, exist_ok=True)
    for c in db.get_content().all():
        if (c.Length or 0) < 120 or not Path(c.FolderPath or "").is_file():
            continue
        m = loudness.measure_file(c.FolderPath)
        entry = {"content_id": c.ID, "title": c.Title, "artist": c.Artist.Name if c.Artist else "",
                 "path": c.FolderPath, "lufs": m.lufs if m else None, "true_peak": m.true_peak if m else None,
                 "original": _snapshot_row(_row(db, c.ID))}
        state["songs"][c.Title] = entry
        for name, title in SONGS.items():
            if title == c.Title:
                state["tracks"][name] = entry
    STATE.write_text(json.dumps(state, indent=2))
    status()
    print("\nNext, in rekordbox 7 (step A — a track with NO MixerParam row):\n"
          "  1. Preferences > Controller > Mixer tab > Auto Gain: Enable.\n"
          f"  2. PERFORMANCE mode: load '{SONGS['R2']}', open the deck's GRID EDIT panel and\n"
          f"     note its Auto Gain value (or that it is blank); then '{SONGS['R3']}'.\n"
          "     (No Performance mode: EXPORT mode, DUAL PLAYER layout, the AUTO GAIN knob.)\n"
          "     Do NOT run Analyze and do NOT turn the knob (a manual change is saved).\n"
          "  3. Quit rekordbox.\n"
          "Then: python dev_gain_kit_g2.py status")


def status() -> None:
    """Every song's MixerParam row now, against -10 - LUFS."""
    db, _ = _db()
    state = json.loads(STATE.read_text())
    print("== songs: MixerParam now   (expected if rekordbox levels to -10 LUFS)")
    for title, t in state["songs"].items():
        r = _row(db, t["content_id"])
        want = f"{-10 - t['lufs']:+.2f} dB" if t["lufs"] is not None else "?"
        if r is None:
            print(f"  {title[:34]:34s} no row               (expect {want})")
        else:
            g, pk = _value(r.GainHigh, r.GainLow), _value(r.PeakHigh, r.PeakLow)
            print(f"  {title[:34]:34s} gain {_db_str(g):>10s}  peak {_db_str(pk):>10s}  (expect {want}; "
                  f"true peak {t['true_peak']:+.2f}; updated {r.updated_at})")


def write() -> None:
    """Step C: R1's gain set to -20 dB (it must have a row: run after rekordbox analysed it)."""
    db, suppressed = _db()
    state = json.loads(STATE.read_text())
    for name, t in state["tracks"].items():
        t["before_write"] = _snapshot_row(_row(db, t["content_id"]))
    if state["tracks"]["R1"]["before_write"] is None:
        raise SystemExit("R1 has no MixerParam row yet — analyse it in rekordbox first.")
    _set_gain(db, state["tracks"]["R1"]["content_id"], -20.0)
    with suppressed():
        db.commit(autoinc=True)
    STATE.write_text(json.dumps(state, indent=2))
    print(f"R1 '{SONGS['R1']}' gain written -20.0 dB.\n"
          "Next, in rekordbox 7 (step C):\n"
          "  1. Load R1 and read GRID EDIT > Auto Gain (or the DUAL PLAYER AUTO GAIN\n"
          "     knob): -20 dB = kept, ~+4.5 dB = recomputed. Do NOT run Analyze.\n"
          f"  2. Export R1, R2, R3 ('{SONGS['R1']}', '{SONGS['R2']}', '{SONGS['R3']}') to a USB stick.\n"
          "  3. Quit rekordbox.\n"
          "Then: python dev_gain_kit_g2.py check1 /Volumes/<STICK>")


def _rows_now(db, state) -> dict:
    return {name: _snapshot_row(_row(db, t["content_id"])) for name, t in state["tracks"].items()}


def _snapshot_stick(stick: Path, dest: Path) -> None:
    rb = stick / "PIONEER/rekordbox"
    if not rb.is_dir():
        raise SystemExit(f"No PIONEER/rekordbox on {stick}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(rb, dest / "rekordbox", ignore=shutil.ignore_patterns("*-shm", "*-wal"))
    shutil.copytree(stick / "PIONEER/USBANLZ", dest / "USBANLZ")


def _report_rows(label: str, rows: dict, state: dict) -> None:
    print(f"\n== master.db MixerParam {label}")
    for name, r in rows.items():
        t = state["tracks"][name]
        orig = t.get("before_write") or t["original"] or {}
        if r is None:
            print(f"  {name}: no row")
            continue
        g, pk = _value(r["GainHigh"], r["GainLow"]), _value(r["PeakHigh"], r["PeakLow"])
        same_id = "same row" if r["ID"] == orig.get("ID") else "NEW row"
        print(f"  {name}: gain {_db_str(g)}, peak {_db_str(pk)}  ({same_id}, updated {r['updated_at']})")


def check1(stick: str) -> None:
    db, _ = _db()
    state = json.loads(STATE.read_text())
    rows = _rows_now(db, state)
    state["after_load"] = rows
    STATE.write_text(json.dumps(state, indent=2))
    _report_rows("after rekordbox loaded + exported (R1 written -20 dB)", rows, state)
    _snapshot_stick(Path(stick), KIT / "stick-1")
    print(f"\nStick snapshot: {KIT / 'stick-1'}\nThen: python dev_gain_kit_g2.py flip")


def flip() -> None:
    db, suppressed = _db()
    state = json.loads(STATE.read_text())
    for name, g in FLIP.items():
        _set_gain(db, state["tracks"][name]["content_id"], g)
    with suppressed():
        db.commit(autoinc=True)
    print("Gains for export 2: " + ", ".join(f"{n} {g:+.1f} dB" for n, g in FLIP.items()) +
          "\n"
          "Next, in rekordbox 7:\n"
          "  1. In the stick's browser, remove R1, R2, R3 from the device.\n"
          "  2. Export R1, R2, R3 to the stick again.\n"
          "  3. Quit rekordbox.\n"
          "Then: python dev_gain_kit_g2.py check2 /Volumes/<STICK>")


def _onelibrary_rows(snap: Path, titles: list[str]) -> dict:
    import sqlcipher3.dbapi2 as sqlcipher
    from pyrekordbox.onelibrary.database import BLOB
    from pyrekordbox.utils import deobfuscate

    db = snap / "rekordbox/exportLibrary.db"
    if not db.is_file():
        return {}
    con = sqlcipher.connect(str(db))
    con.execute(f"PRAGMA key='{deobfuscate(BLOB)}'")
    cols = [r[1] for r in con.execute("PRAGMA table_info(content)")]
    out = {}
    for row in con.execute("SELECT * FROM content"):
        d = dict(zip(cols, row))
        if d.get("title") in titles:
            out[d["title"]] = d
    con.close()
    return out


def _pdb_rows(snap: Path, titles: list[str]) -> dict:
    from konduktor.adapters.rekordbox import pdb

    f = snap / "rekordbox/export.pdb"
    if not f.is_file():
        return {}
    buf = f.read_bytes()
    out = {}
    for row in pdb.table_rows(buf, pdb.TRACKS):
        fields, strings = pdb.track_fields(row)
        if strings.get("title") in titles:
            out[strings["title"]] = (row[: pdb.track_extent(row)], fields, strings)
    return out


def _anlz(snap: Path, rel: str | None) -> dict:
    from konduktor.adapters.rekordbox import anlz_file

    out = {}
    if not rel:
        return out
    base = snap / "USBANLZ" / Path(rel.lstrip("/")).relative_to("PIONEER/USBANLZ")
    for ext in (".DAT", ".EXT", ".2EX"):
        p = base.with_suffix(ext)
        if p.is_file():
            tags, seen = {}, {}
            for t in anlz_file.parse_file(p).tags:  # PCOB / PCO2 come twice (hot, memory)
                seen[t.type] = seen.get(t.type, 0) + 1
                tags[f"{t.type}#{seen[t.type]}"] = t.data
            out[ext] = tags
    return out


def check2(stick: str) -> None:
    db, _ = _db()
    state = json.loads(STATE.read_text())
    rows = _rows_now(db, state)
    _report_rows("before export 2 (flip), as rekordbox left it", rows, state)
    _snapshot_stick(Path(stick), KIT / "stick-2")
    titles = [t["title"] for t in state["tracks"].values()]
    s1, s2 = KIT / "stick-1", KIT / "stick-2"

    print("\n== OneLibrary content rows: columns that differ between export 1 and 2")
    a, b = _onelibrary_rows(s1, titles), _onelibrary_rows(s2, titles)
    ids = {"content_id", "analysisDataFilePath", "image_id", "path", "dateAdded"}
    for title in titles:
        if title not in a or title not in b:
            print(f"  {title}: missing in an export ({title in a}, {title in b})")
            continue
        diff = {k: (a[title][k], b[title][k]) for k in a[title] if a[title][k] != b[title].get(k)}
        print(f"  {title[:40]}: {diff or 'identical'}")

    print("\n== export.pdb track rows: fields / bytes that differ")
    a, b = _pdb_rows(s1, titles), _pdb_rows(s2, titles)
    for title in titles:
        if title not in a or title not in b:
            print(f"  {title}: missing in a pdb ({title in a}, {title in b})")
            continue
        fa, fb = a[title][1], b[title][1]
        diff = {k: (fa[k], fb[k]) for k in fa if fa[k] != fb.get(k)}
        sdiff = {k: (a[title][2][k], b[title][2][k]) for k in a[title][2] if a[title][2][k] != b[title][2].get(k)}
        print(f"  {title[:40]}: fields {diff or 'identical'}; strings {sdiff or 'identical'}")

    print("\n== ANLZ tags that differ")
    oa, ob = _onelibrary_rows(s1, titles), _onelibrary_rows(s2, titles)
    for title in titles:
        ta = _anlz(s1, (oa.get(title) or {}).get("analysisDataFilePath"))
        tb = _anlz(s2, (ob.get(title) or {}).get("analysisDataFilePath"))
        changed = [f"{ext}:{k}" for ext in ta for k in ta[ext] if ta[ext][k] != tb.get(ext, {}).get(k)]
        missing = [f"{ext}:{k}" for ext in tb for k in tb[ext] if k not in ta.get(ext, {})]
        print(f"  {title[:40]}: changed {changed or 'none'}; new {missing or 'none'}")
    print("\nThen: python dev_gain_kit_g2.py restore")


def restore() -> None:
    """R1 and R3 back to the gain rekordbox computed (their rows before `write`)."""
    db, suppressed = _db()
    state = json.loads(STATE.read_text())
    for name, t in state["tracks"].items():
        o, r = t.get("before_write"), _row(db, t["content_id"])
        if o and r is not None:
            r.GainHigh, r.GainLow, r.PeakHigh, r.PeakLow = o["GainHigh"], o["GainLow"], o["PeakHigh"], o["PeakLow"]
    with suppressed():
        db.commit(autoinc=True)
    print("R1-R3 gains restored to rekordbox's own values.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    fn = {"prepare": prepare, "status": status, "write": write, "check1": check1, "flip": flip,
          "check2": check2, "restore": restore}[cmd]
    fn(*sys.argv[2:])
