#!/usr/bin/env bash
# Set up and start FHIR Record Retrieval on macOS / Linux.
#   ./run.sh    ./run.sh --dev (auto-reload)    ./run.sh --mock    ./run.sh --test    ./run.sh --check
set -euo pipefail
cd "$(dirname "$0")"

PY=$(command -v python3 || command -v python)
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10+ is required"; exit 1; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv

stamp=.venv/requirements.stamp
hash=$(cksum requirements.txt | cut -d' ' -f1)
if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$hash" ]; then
  echo "Installing requirements ..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  echo "$hash" > "$stamp"
fi

[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example - add AOAI_ENDPOINT and AOAI_API_KEY to use Azure OpenAI."; }

case "${1:-}" in
  --test) exec .venv/bin/python -m pytest -q tests ;;
  --check) exec .venv/bin/python -m dev.check ;;
esac

env_value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | sed 's/#.*//' | xargs; }
# Environment variables win over .env, e.g. APP_PORT=8080 ./run.sh
HOST=${APP_HOST:-$(env_value APP_HOST)}; HOST=${HOST:-127.0.0.1}
PORT=${APP_PORT:-$(env_value APP_PORT)}; PORT=${PORT:-8000}

if [ "${1:-}" = "--mock" ]; then
  echo "Starting the offline mock FHIR server on :9000 (set FHIR_BASE=http://localhost:9000/r4/mock in .env)"
  .venv/bin/python -m uvicorn dev.mock_fhir:app --port 9000 &
  trap 'kill %1 2>/dev/null' EXIT
fi

ARGS=(-m uvicorn agent.app:app --host "$HOST" --port "$PORT")
[ "${1:-}" = "--dev" ] && ARGS+=(--reload)
echo "Open http://$HOST:$PORT   (Ctrl+C to stop)"
.venv/bin/python "${ARGS[@]}"
