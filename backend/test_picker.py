"""The library picker's routes: platform first, then library.

Worth pinning because the bug this flow replaced was invisible. `/api/library/
options` used to flatten every driver's detections and return `[0]`, so "the
best candidate" actually meant "whatever the first-registered driver found" —
and since adapters register in import order, a plugged-in USB stick could be
offered as the user's *collection*. Nothing failed; it just opened the wrong
library. The guarantee now is scoping, and scoping is easy to lose again the
next time a route grows a shortcut.

Runs on whatever this machine has: every assertion is about the SHAPE of the
answer, so a laptop with no Traktor, no Rekordbox and nothing plugged in still
exercises the same rules. Prefs are redirected to a temp file — a test must
never rewrite the user's last-opened library.
"""
import tempfile
from pathlib import Path

from konduktor import prefs

# Redirect prefs BEFORE anything reads them.
_TMP = tempfile.TemporaryDirectory()
prefs._PREFS_PATH = Path(_TMP.name) / "userprefs.json"
prefs._LEGACY_PREFS_PATH = Path(_TMP.name) / "absent.json"

from konduktor.core import places, registry  # noqa: E402
from konduktor.main import collection_options, fs_list, fs_places, platforms  # noqa: E402

failed = False


def check(label, cond, detail=""):
    global failed
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failed = True


opts = platforms()
by_name = {p.platform: p for p in opts}

print("== /api/platforms reports the facts the platform step needs ==")
check("every registered driver is offered", len(opts) == len(registry.drivers()),
      f"{len(opts)} of {len(registry.drivers())}")
check("all three platforms present", {"traktor", "rekordbox", "onelibrary"} <= set(by_name))
check("selects is per-platform, not assumed",
      by_name["traktor"].selects == "file"
      and by_name["rekordbox"].selects == "file"
      # A OneLibrary library is the DRIVE. Getting this wrong sends the browser
      # hunting for a file three levels down that nobody would navigate to.
      and by_name["onelibrary"].selects == "directory",
      {p: by_name[p].selects for p in by_name})
check("only OneLibrary is removable",
      by_name["onelibrary"].removable
      and not by_name["traktor"].removable
      and not by_name["rekordbox"].removable)
check("installed agrees with found", all((p.found > 0) == p.installed for p in opts))
# The list is a MENU. Registration order is an import-order accident, so sorting
# is what stops it reshuffling between two launches.
check("ordered fixed-location first, then by name",
      [p.platform for p in opts] == [p.platform for p in
                                     sorted(opts, key=lambda o: (o.removable, o.name.lower()))],
      [p.platform for p in opts])
check("a second call gives the same order", [p.platform for p in platforms()] == [p.platform for p in opts])

print("== /api/library/options is scoped to ONE platform ==")
for plat in ("traktor", "rekordbox", "onelibrary"):
    o = collection_options(plat)
    driver = registry.driver_by_platform(plat)
    own = {c["path"] for c in registry.detect_for(plat)}
    got = {c.path for c in o.detected}
    check(f"{plat}: detected is exactly that platform's own", got == own, f"{got} vs {own}")
    check(f"{plat}: auto is the first detected",
          (o.auto.path if o.auto else None) == (o.detected[0].path if o.detected else None))
    # The old bug, stated directly: nothing removable may surface under a
    # platform that keeps its library in a fixed place.
    if not getattr(driver, "removable", False):
        removable_paths = {c["path"] for d in registry.drivers()
                           if getattr(d, "removable", False) for c in d.detect()}
        check(f"{plat}: no removable drive leaks in", not (got & removable_paths))

check("unscoped still answers (nothing depends on it, but it must not raise)",
      collection_options() is not None)
check("an unknown platform detects nothing rather than raising",
      collection_options("nonesuch").detected == [])

print("== per-platform 'open last' does not cross platforms ==")
prefs.set_last_collection("/tmp/a/collection.nml", "traktor")
prefs.set_last_collection("/tmp/b/master.db", "rekordbox")
check("traktor remembers its own", prefs.get_last_collection("traktor") == "/tmp/a/collection.nml")
check("rekordbox remembers its own", prefs.get_last_collection("rekordbox") == "/tmp/b/master.db")
# Offering a Rekordbox master.db to someone who just chose Traktor is an offer
# that cannot be taken, so an unknown platform gets None, NOT the global value.
check("a platform with no record gets None, not the global one",
      prefs.get_last_collection("onelibrary") is None
      and prefs.get_last_collection() == "/tmp/b/master.db")
check("the picker surfaces it as `recent`",
      (collection_options("traktor").recent or None) is not None
      and collection_options("traktor").recent.path == "/tmp/a/collection.nml")
check("a remembered library that has gone is offered as exists=False",
      collection_options("traktor").recent.exists is False)

print("== /api/fs/list only lists what the chosen platform can open ==")
root = Path(tempfile.mkdtemp())
(root / "collection.nml").write_text("x")
(root / "master.db").write_bytes(b"x")
(root / "notes.txt").write_text("x")
(root / "SomeDrive").mkdir()

names = lambda plat: {f.name for f in fs_list(str(root), plat).files}  # noqa: E731
check("traktor sees only .nml", names("traktor") == {"collection.nml"}, names("traktor"))
check("rekordbox sees only .db", names("rekordbox") == {"master.db"}, names("rekordbox"))
# Not "the .db files it might contain": its library is a folder, so listing any
# file would offer something the Open action cannot act on.
check("onelibrary, which picks a folder, sees no files at all", names("onelibrary") == set())
check("unscoped still sees the union", names(None) == {"collection.nml", "master.db"})
check("never the unopenable", all("notes.txt" not in names(p)
                                 for p in (None, "traktor", "rekordbox", "onelibrary")))
check("folders are listed for every platform",
      all({d.name for d in fs_list(str(root), p).dirs} == {"SomeDrive"}
          for p in (None, "traktor", "rekordbox", "onelibrary")))

print("== /api/fs/places: the browser's sidebar ==")
rows = fs_places()
check("there is always somewhere to go", len(rows) > 0)
# A greyed-out Music folder on a machine that has none reads as breakage; its
# absence reads as an accurate description of the machine.
check("every place exists and is a directory",
      all(Path(r.path).is_dir() for r in rows),
      [r.path for r in rows if not Path(r.path).is_dir()])
check("home is always offered", any(r.kind == "home" for r in rows))
check("no duplicate paths", len({r.path for r in rows}) == len(rows))
check("nothing is unnamed", all(r.name for r in rows))
# macOS lists the startup disk in /Volumes next to real removable media, so a
# Drives list that does not filter it leads with a path back to where you are.
# Compared by DEVICE, because "Macintosh HD" is only the default name.
check("the boot volume is not offered as a drive",
      not any(places.is_boot_volume(Path(r.path)) for r in rows if r.kind == "volume"),
      [r.path for r in rows if r.kind == "volume" and places.is_boot_volume(Path(r.path))])

lib_places = lambda plat: [r for r in fs_places(plat) if r.kind == "library"]  # noqa: E731
check("no library shortcut without a platform", lib_places(None) == [])
for plat in ("traktor", "rekordbox"):
    found = registry.detect_for(plat)
    expected = {str(Path(c["path"]).parent) for c in found}
    got = {r.path for r in lib_places(plat)}
    # The shortcut is the folder CONTAINING the library: a place is somewhere
    # you navigate to, not something you pick.
    check(f"{plat}: shortcut points at the containing folder", got <= expected, f"{got} vs {expected}")
    if found:
        check(f"{plat}: the shortcut is offered", len(got) > 0)
# A removable platform's libraries ARE the drives, which the Drives group
# already lists — a second row for the same place would just be noise.
check("no library shortcut for a removable platform", lib_places("onelibrary") == [])
check("an unknown platform does not raise", isinstance(fs_places("nonesuch"), list))

print("== the mount scanner is shared, not duplicated ==")
# It grew inside the OneLibrary adapter, because that is the platform whose
# libraries are drives. "Where does this OS mount things" is not adapter
# knowledge, though, and two scanners would drift.
import konduktor.adapters.onelibrary.discovery as onelib_discovery  # noqa: E402
check("the adapter uses core's scanner, not its own",
      onelib_discovery._mount_points is places.mount_points)
check("volumes() drops the boot volume, mount_points() does not",
      set(places.volumes()) <= set(places.mount_points()))

print("== a library may be a DIRECTORY, not just a file ==")
drives = registry.detect_for("onelibrary")
if drives:
    # `describe()` returns the database, but the drive root is what a user picks
    # in the browser. Both must name the same library or the picker and the
    # last-opened shortcut would disagree about what is valid.
    db = Path(drives[0]["path"])
    root_dir = db.parent.parent.parent
    driver = registry.driver_by_platform("onelibrary")
    check("the mount point opens", driver.can_open(root_dir), str(root_dir))
    check("so does the database it points at", driver.can_open(db))
    # ...and both must be CALLED the same thing. The sidebar names the open
    # library, and a filename would call one stick two different names purely
    # by which end of it the user happened to click.
    check("both ends of the drive give one display name",
          driver.display_name_for(root_dir) == driver.display_name_for(db) == root_dir.name,
          f"{driver.display_name_for(root_dir)!r} vs {driver.display_name_for(db)!r}")
    check("a path that is not a drive falls back to its own name",
          driver.display_name_for(Path("/nope/whatever.db")) == "whatever.db")
else:
    print("  [SKIP] no OneLibrary drive connected")

print("\n" + ("❌ FAILED" if failed else "✅ PASSED"))
_TMP.cleanup()
raise SystemExit(1 if failed else 0)
