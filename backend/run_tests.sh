#!/usr/bin/env bash
# Run Konduktor's backend tests against a copy of the real collection.
# ALWAYS run this before changing anything in the save/serialization path.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source .venv/bin/activate

fail=0
for t in test_save_fidelity.py test_phase3.py; do
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
