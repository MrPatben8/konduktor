#!/usr/bin/env bash
# Start both Konduktor servers (backend + frontend) together.
# Ctrl-C stops both. First run? See one-time setup in README.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Backend (FastAPI on :8000)
source "$ROOT/backend/.venv/bin/activate"
uvicorn konduktor.main:app --reload --port 8000 --app-dir "$ROOT/backend" &
BACKEND_PID=$!

# Frontend (Vite on :5173, proxies /api -> :8000)
(cd "$ROOT/frontend" && npm run dev) &
FRONTEND_PID=$!

# Stop both on exit.
trap 'kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true' EXIT INT TERM

echo ""
echo "  Konduktor is starting…"
echo "  → App:      http://localhost:5173"
echo "  → API docs: http://localhost:8000/docs"
echo "  (Ctrl-C to stop both)"
echo ""
wait
