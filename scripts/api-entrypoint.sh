#!/bin/sh
# Container entrypoint for jeen-insights-api.
#
# RUN_MIGRATIONS_ON_START=true applies db/migrations/insights/*.sql before the
# server starts, so a fresh deployment needs no separate migration step. The
# runner serialises itself with a PostgreSQL advisory lock, so several replicas
# starting at once do not race. A failed migration exits non-zero and the pod
# restarts instead of serving against a half-migrated schema.
#
# Anything else passed as arguments is exec'd as-is (the image CMD, or an
# ad-hoc command such as the migration script itself).
set -eu

case "$(printf '%s' "${RUN_MIGRATIONS_ON_START:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    echo "entrypoint: applying schema migrations (RUN_MIGRATIONS_ON_START=${RUN_MIGRATIONS_ON_START})"
    python scripts/run_insights_migrations.py
    ;;
esac

exec "$@"
