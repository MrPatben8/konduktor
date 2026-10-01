#!/usr/bin/env bash
# Run Konduktor's backend tests against a copy of the real collection.
# ALWAYS run this before changing anything in the save/serialization path.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source .venv/bin/activate

fail=0
for t in test_layering.py test_save_fidelity.py test_timebase.py test_stem_file.py test_stem_swap.py test_engine_manager.py test_stems_batch.py test_stem_playback.py test_traktor_adapter.py test_rekordbox_adapter.py test_rekordbox_fidelity.py test_onelibrary_adapter.py test_onelibrary_fidelity.py test_onelibrary_anlz.py test_onelibrary_pdb.py test_onelibrary_tracks.py test_import.py test_grid_detect.py test_auto_hotcues.py test_grid_batch.py test_bulk_remove.py test_folders.py test_picker.py test_relocate.py test_library_id.py test_exports.py test_export.py test_export_pioneer.py test_device_export.py test_phase3.py test_history.py test_discard.py; do
  echo "──────────────────────────────────────────"
  echo "▶ $t"
  echo "──────────────────────────────────────────"
  python "$t" || fail=1
  echo
done

if [ "$fail" -ne 0 ]; then
  echo "❌ TESTS FAILED"
  exit 1
fi
echo "✅ ALL TESTS PASSED"
