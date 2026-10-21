#!/usr/bin/env bash
# ─── AOI Local CI ─────────────────────────────────────────────────────────────
#
# Runs the same checks as .github/workflows/ci.yml, locally. "CI green" means
# this script exits 0; it is the merge gate (ADR 0034).
#
# Usage:
#   ./scripts/ci.sh
#
# Prerequisites:
#   - Dev containers running: docker compose up -d postgres seaweedfs
#   - .env with AOI_TEST_DATABASE_URL pointing at the aoi_test database
#
# The migration round-trip downgrades the TEST database to base and back.
# It refuses to run unless the URL names aoi_test.

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=".env"

cd "$PROJECT_DIR"

# ─── Helpers ──────────────────────────────────────────────────────────────────

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[PASS]\033[0m  $*"; }
fatal() { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; exit 1; }

step() {
    local name="$1"
    shift
    info "$name"
    "$@" || fatal "$name"
    ok "$name"
}

# ─── Preconditions ────────────────────────────────────────────────────────────

[ -f "$ENV_FILE" ] || fatal "$ENV_FILE not found — copy .env.example and fill it in"
set -a
# shellcheck disable=SC1090
. "./$ENV_FILE"
set +a

case "${AOI_TEST_DATABASE_URL:-}" in
    *aoi_test*) ;;
    *) fatal "AOI_TEST_DATABASE_URL must point at the aoi_test database" ;;
esac

uv run python -c "
import os
from sqlalchemy import create_engine, text
with create_engine(os.environ['AOI_TEST_DATABASE_URL']).connect() as c:
    c.execute(text('select 1'))
" >/dev/null 2>&1 || fatal "test database unreachable — is the postgres container up?"

curl -s -o /dev/null --max-time 5 "${AOI_S3_ENDPOINT:-http://localhost:8333}" \
    || fatal "S3 endpoint unreachable — is the seaweedfs container up?"

# ─── Checks (same order as ci.yml) ────────────────────────────────────────────

migration_round_trip() {
    AOI_DATABASE_URL="$AOI_TEST_DATABASE_URL" uv run alembic upgrade head \
        && AOI_DATABASE_URL="$AOI_TEST_DATABASE_URL" uv run alembic downgrade base \
        && AOI_DATABASE_URL="$AOI_TEST_DATABASE_URL" uv run alembic upgrade head
}

step "Lockfile matches pyproject" uv lock --check
step "Lint"                       uv run ruff check .
step "Format check"               uv run ruff format --check .
step "Type check"                 uv run mypy aoi/
step "Migration round-trip"       migration_round_trip
step "Test"                       uv run pytest tests/ -q -m "not live" --cov=aoi --cov-report=term-missing

echo -e "\033[1;32mCI green.\033[0m"
