#!/usr/bin/env bash
# Start Konduktor for hands-on testing, protecting what actually needs it.
#
# Clicking around the real app means clicking real Save buttons, so the TRAKTOR
# collection is copied into a throwaway sandbox — it is 8,485 entries of real,
# irreplaceable library. The REKORDBOX library is opened live, because it is a
# disposable test library kept for exactly this.
#
# Prefs and version history always go to a temp directory, so testing never
# disturbs the real ones. Ctrl-C stops both servers.
#
# Override either source with KONDUKTOR_TRAKTOR_SRC / KONDUKTOR_REKORDBOX_SRC,
# or set KONDUKTOR_COPY_REKORDBOX=1 to sandbox Rekordbox too.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Refuse to start if something is already SERVING on the ports.
#
# This is load-bearing, not a nicety: if :8000 is taken, this script's backend
# dies quietly and the browser talks to whatever was already there — quite
# possibly a server holding the real Traktor collection. The window would still
# claim to be a sandbox while every Save went somewhere else entirely.
#
# `-sTCP:LISTEN` matters: a plain `lsof -ti:PORT` also matches CLIENT sockets, so
# a browser tab left open on the old app looks exactly like a running server and
# blocks startup for no reason.
for port_and_what in "8000:backend" "5173:frontend"; do
  port="${port_and_what%%:*}"
  what="${port_and_what##*:}"
  if lsof -ti:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    cat >&2 <<EOF

  Refusing to start: port $port is already in use (the $what's port).

  Another Konduktor is probably running. If this one started anyway its backend
  would die quietly and the browser would talk to THAT one instead — including
  its Traktor collection, which is not a sandbox copy.

  Stop the other one first (Ctrl-C in its terminal), or find it with:
      lsof -ti:$port -sTCP:LISTEN

EOF
    exit 1
  fi
done

SANDBOX="${KONDUKTOR_SANDBOX:-${TMPDIR:-/tmp}/konduktor-sandbox}"
rm -rf "$SANDBOX"
mkdir -p "$SANDBOX/data"

echo "  Building a sandbox in $SANDBOX …"

source "$ROOT/backend/.venv/bin/activate"

# --- Traktor: a copy of the collection ---------------------------------------
#
# Found with the app's OWN detection rather than a hard-coded path, so this
# always tests what the app would actually open. The repo root also contains a
# collection.nml, which is a stale fixture — defaulting to that silently tested
# months-old data.
detect_traktor() {
  # PYTHONPATH matters: the package lives in backend/, not at the repo root, and
  # without it this silently prints nothing and the sandbox starts with no
  # library at all.
  PYTHONPATH="$ROOT/backend" python - <<'PY'
from konduktor.adapters.traktor import discovery

found = [c for c in discovery.detect_collections() if c.get("exists")]
if found:
    print(found[0]["path"])
PY
}
TRAKTOR_SRC="${KONDUKTOR_TRAKTOR_SRC:-$(detect_traktor)}"
TRAKTOR=""
if [ -n "$TRAKTOR_SRC" ] && [ -f "$TRAKTOR_SRC" ]; then
  mkdir -p "$SANDBOX/traktor"
  cp "$TRAKTOR_SRC" "$SANDBOX/traktor/collection.nml"
  TRAKTOR="$SANDBOX/traktor/collection.nml"
  echo "    Traktor:   copy of $TRAKTOR_SRC"
  echo "               (modified $(date -r "$TRAKTOR_SRC" '+%Y-%m-%d %H:%M'))"
else
  echo "    Traktor:   (no collection detected — set KONDUKTOR_TRAKTOR_SRC)"
fi

# --- Rekordbox: the live library (a disposable test library) ------------------
RB_SRC="${KONDUKTOR_REKORDBOX_SRC:-$HOME/Library/Pioneer/rekordbox}"
REKORDBOX=""
if [ -f "$RB_SRC/master.db" ]; then
  if [ -n "${KONDUKTOR_COPY_REKORDBOX:-}" ]; then
    mkdir -p "$SANDBOX/rekordbox"
    # Copy the database WITH its sidecars: a stale -wal left beside a replaced
    # .db is replayed onto it by SQLite and corrupts it. And the beatgrids live
    # in share/, so a master.db copied alone has none.
    for f in master.db master.db-wal master.db-shm masterPlaylists6.xml; do
      [ -e "$RB_SRC/$f" ] && cp "$RB_SRC/$f" "$SANDBOX/rekordbox/"
    done
    [ -d "$RB_SRC/share" ] && cp -R "$RB_SRC/share" "$SANDBOX/rekordbox/share"
    REKORDBOX="$SANDBOX/rekordbox/master.db"
    echo "    Rekordbox: $REKORDBOX (copied)"
  else
    REKORDBOX="$RB_SRC/master.db"
    echo "    Rekordbox: $REKORDBOX (LIVE — disposable test library)"
  fi
else
  echo "    Rekordbox: (none found at $RB_SRC)"
fi

# Prefs + version history land here, so the sandbox cannot disturb real ones.
export KONDUKTOR_DATA_DIR="$SANDBOX/data"
# Open one on startup so the app lands straight in the library.
if [ -n "$TRAKTOR" ]; then
  export KONDUKTOR_NML="$TRAKTOR"
elif [ -n "$REKORDBOX" ]; then
  export KONDUKTOR_NML="$REKORDBOX"
fi

uvicorn konduktor.main:app --reload --port 8000 --app-dir "$ROOT/backend" &
BACKEND_PID=$!
(cd "$ROOT/frontend" && npm run dev) &
FRONTEND_PID=$!
trap 'kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true' EXIT INT TERM

cat <<EOF

  Konduktor (test mode)
  → App:      http://localhost:5173
  → API docs: http://localhost:8000/docs

  Switch libraries in the app with "Find manually" and point it at:
EOF
[ -n "$TRAKTOR" ]   && echo "    $TRAKTOR"
[ -n "$REKORDBOX" ] && echo "    $REKORDBOX"
cat <<EOF

  Traktor is a sandbox copy. Rekordbox is LIVE, by design.
  Delete the sandbox when done:  rm -rf "$SANDBOX"
  (Ctrl-C to stop both servers)

EOF
wait
