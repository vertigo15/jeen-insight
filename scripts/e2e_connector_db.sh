#!/usr/bin/env bash
# Reproducible DB-backed end-to-end harness for the agent-tools connector gate.
#
# Spins up an EPHEMERAL Postgres (never the shared prod metadata DB), applies all
# insights migrations (001..019, incl. agent-tools 017/018/019), and runs the
# real propose -> preview -> execute (+continue) flow against it with only
# external egress stubbed (see tests/integration/test_connector_gate_db.py).
#
# Usage:
#   scripts/e2e_connector_db.sh          # up + migrate + test (leaves DB running)
#   scripts/e2e_connector_db.sh --down   # tear the ephemeral DB down
#
# Requires: docker, python3 with the project deps installed. The project .env
# supplies the non-DB settings (Azure creds etc.); this script OVERRIDES only the
# METADATA_DB_* vars (pydantic reads OS env before .env) to point at the throwaway
# DB, and sets a strong test APP_ENCRYPTION_KEY.
set -euo pipefail

cd "$(dirname "$0")/.."

CONTAINER="${E2E_PG_CONTAINER:-jeen-e2e-pg}"
PORT="${E2E_PG_PORT:-55440}"
KEK_FILE="${E2E_KEK_FILE:-/tmp/jeen_e2e_kek.txt}"

if [[ "${1:-}" == "--down" ]]; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  echo "removed $CONTAINER"
  exit 0
fi

# 1) Ephemeral Postgres (idempotent: recreate a clean one each run).
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_USER=e2e -e POSTGRES_PASSWORD=e2e -e POSTGRES_DB=e2e \
  -p "${PORT}:5432" postgres:16-alpine >/dev/null
echo "started $CONTAINER on localhost:${PORT}"

# 2) Wait for readiness.
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U e2e >/dev/null 2>&1; then break; fi
  sleep 1
done

# 3) The app normally creates app_settings at startup (_ensure_schema) BEFORE the
#    migration runner runs via docker exec. This standalone harness has no app
#    boot, so create that one prerequisite table here.
docker exec "$CONTAINER" psql -U e2e -d e2e -c \
  "CREATE TABLE IF NOT EXISTS app_settings (key VARCHAR PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ DEFAULT NOW());" >/dev/null

# 4) Strong test KEK (reuse a saved one so re-runs can decrypt prior rows).
if [[ ! -s "$KEK_FILE" ]]; then
  python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" > "$KEK_FILE"
fi

export APP_ENCRYPTION_KEY="$(cat "$KEK_FILE")"
export METADATA_DB_HOST=localhost METADATA_DB_PORT="$PORT" METADATA_DB_NAME=e2e \
       METADATA_DB_USER=e2e METADATA_DB_PASSWORD=e2e METADATA_DB_SSL=false \
       JEEN_DEV_MODE=true

# 5) Apply all migrations (records revisions; safe to re-run).
python3 scripts/run_insights_migrations.py

# 6) Run the DB-backed e2e.
export JEEN_E2E_DB=1
python3 -m pytest tests/integration/test_connector_gate_db.py -q -p no:cacheprovider

echo "e2e complete. Tear down with: scripts/e2e_connector_db.sh --down"
