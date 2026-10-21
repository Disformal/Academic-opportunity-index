#!/usr/bin/env bash
# ─── AOI Production Stack Integration Test ────────────────────────────────────
#
# Boots the full production Docker Compose stack locally and verifies that all
# services wire together correctly. Run before any VPS deployment.
#
# Usage:
#   ./scripts/integration-test.sh
#
# Prerequisites:
#   - Docker and Docker Compose v2 installed
#   - Port 80 free (nginx binds to it)
#   - No running AOI dev containers on conflicting ports
#
# The script generates temporary credentials, starts all services, runs tests,
# and tears everything down. Nothing persists after it finishes.

set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="docker-compose.prod.yml"
# Own compose project, so `down -v` can only remove this stack's volumes. Without it the
# project defaults to the directory name, shared with the dev stack, and on 2026-10-07 the
# cleanup deleted the dev evidence store (opportunity-index_seaweedfs_data).
# Exported so every compose call, including those inside backup.sh, uses it.
export COMPOSE_PROJECT_NAME="aoi-integration"
ENV_FILE=".env"
ENV_BACKUP=".env.bak-integration-test"
HTPASSWD_FILE="nginx/htpasswd"
TEST_USER="testadmin"
TEST_PASS="$(openssl rand -hex 12)"
HEALTH_TIMEOUT=180  # seconds

PASSED=0
FAILED=0
HTPASSWD_CREATED=false
ENV_BACKED_UP=false

cd "$PROJECT_DIR"

# ─── Helpers ──────────────────────────────────────────────────────────────────

info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[PASS]\033[0m  $*"; PASSED=$((PASSED + 1)); }
fail()  { echo -e "\033[1;31m[FAIL]\033[0m  $*"; FAILED=$((FAILED + 1)); }
fatal() { echo -e "\033[1;31m[FATAL]\033[0m $*" >&2; exit 1; }

run_test() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        ok "$name"
    else
        fail "$name"
    fi
}

# ─── Cleanup (runs on EXIT, including errors) ─────────────────────────────────

cleanup() {
    info "Cleaning up..."
    docker compose -f "$COMPOSE_FILE" down -v 2>/dev/null || true
    # Restore original .env if we backed it up
    if [ "$ENV_BACKED_UP" = true ] && [ -f "$ENV_BACKUP" ]; then
        mv "$ENV_BACKUP" "$ENV_FILE"
        info "Restored original $ENV_FILE"
    else
        rm -f "$ENV_FILE"
    fi
    if [ "$HTPASSWD_CREATED" = true ] && [ -f "$HTPASSWD_FILE" ]; then
        rm -f "$HTPASSWD_FILE"
    fi
    # Remove test backup files
    rm -f backups/aoi-*-integration-test.dump 2>/dev/null || true
    info "Cleanup complete."
}
trap cleanup EXIT

# ─── Pre-flight checks ───────────────────────────────────────────────────────

info "Running pre-flight checks..."

# Docker Compose available?
if ! docker compose version >/dev/null 2>&1; then
    fatal "docker compose is not available. Install Docker Compose v2."
fi

# Compose file exists?
if [ ! -f "$COMPOSE_FILE" ]; then
    fatal "$COMPOSE_FILE not found. Run from the project root."
fi

# Port 80 free?
if ss -tlnp 2>/dev/null | grep -q ':80 ' || lsof -i :80 >/dev/null 2>&1; then
    fatal "Port 80 is in use. Stop the process occupying it before running this test."
fi

info "Pre-flight checks passed."

# ─── Generate temporary .env ─────────────────────────────────────────────────

# Back up existing .env if present
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$ENV_BACKUP"
    ENV_BACKED_UP=true
    info "Backed up existing $ENV_FILE to $ENV_BACKUP"
fi

info "Generating temporary environment file: $ENV_FILE"

PG_PASS="$(openssl rand -hex 16)"
GRAFANA_PASS="$(openssl rand -hex 16)"
SECRET_KEY="$(openssl rand -hex 48)"

cat > "$ENV_FILE" <<EOF
# Auto-generated for integration testing — do not commit
POSTGRES_PASSWORD=${PG_PASS}
GRAFANA_ADMIN_PASSWORD=${GRAFANA_PASS}
AOI_DATABASE_URL=postgresql+psycopg://aoi:${PG_PASS}@localhost:5432/aoi
AOI_S3_ENDPOINT=http://localhost:8333
AOI_S3_ACCESS_KEY=minioadmin
AOI_S3_SECRET_KEY=minioadmin
AOI_SECRET_KEY=${SECRET_KEY}
AOI_DEBUG=false
AOI_LOG_LEVEL=INFO
EOF

# ─── Generate nginx htpasswd ─────────────────────────────────────────────────

if [ -f "$HTPASSWD_FILE" ]; then
    info "Using existing $HTPASSWD_FILE"
else
    info "Generating $HTPASSWD_FILE"
    mkdir -p "$(dirname "$HTPASSWD_FILE")"
    if command -v htpasswd >/dev/null 2>&1; then
        htpasswd -bc "$HTPASSWD_FILE" "$TEST_USER" "$TEST_PASS"
    else
        # Fallback: generate with openssl
        HASHED="$(openssl passwd -apr1 "$TEST_PASS")"
        echo "${TEST_USER}:${HASHED}" > "$HTPASSWD_FILE"
    fi
    HTPASSWD_CREATED=true
fi

# ─── Build and start ─────────────────────────────────────────────────────────

info "Building and starting production stack..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build

# ─── Wait for all services to be healthy ──────────────────────────────────────

CONTAINERS=("aoi-postgres" "aoi-seaweedfs" "aoi-app" "aoi-grafana" "aoi-nginx")

info "Waiting for all containers to become healthy (timeout: ${HEALTH_TIMEOUT}s)..."

elapsed=0
while [ $elapsed -lt $HEALTH_TIMEOUT ]; do
    all_healthy=true
    for container in "${CONTAINERS[@]}"; do
        status=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "not-found")
        if [ "$status" != "healthy" ] && [ "$status" != "no-healthcheck" ]; then
            all_healthy=false
            break
        fi
    done

    if [ "$all_healthy" = true ]; then
        info "All containers healthy after ${elapsed}s."
        break
    fi

    sleep 5
    elapsed=$((elapsed + 5))
done

if [ "$all_healthy" != true ]; then
    echo ""
    info "Container status after timeout:"
    for container in "${CONTAINERS[@]}"; do
        status=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "not-found")
        echo "  $container: $status"
    done
    echo ""
    info "Logs from unhealthy containers:"
    for container in "${CONTAINERS[@]}"; do
        status=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container" 2>/dev/null || echo "not-found")
        if [ "$status" = "unhealthy" ]; then
            echo "  --- $container logs (last 30 lines) ---"
            docker logs "$container" --tail 30 2>&1 | sed 's/^/    /'
        fi
    done
    fatal "Not all containers became healthy within ${HEALTH_TIMEOUT}s."
fi

# ─── Run migrations and seed ─────────────────────────────────────────────────

info "Running Alembic migrations..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T app alembic upgrade head

info "Seeding sources..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T app python scripts/seed_sources.py

# ─── Tests ────────────────────────────────────────────────────────────────────

echo ""
info "Running integration tests..."
echo ""

# T1: Unauthenticated access blocked
run_test "T1  Unauthenticated access returns 401" \
    test "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:80/)" = "401"

# T2: Authenticated access works
run_test "T2  Authenticated access returns 200" \
    test "$(curl -s -o /dev/null -w '%{http_code}' -u "${TEST_USER}:${TEST_PASS}" http://localhost:80/)" = "200"

# T3: Health endpoint
T3_BODY="$(curl -sf -u "${TEST_USER}:${TEST_PASS}" http://localhost:80/health 2>/dev/null || echo "")"
if echo "$T3_BODY" | grep -q '"status"'; then
    ok "T3  Health endpoint returns status JSON"
else
    fail "T3  Health endpoint returns status JSON (got: $T3_BODY)"
fi

# T4: Grafana accessible through nginx
run_test "T4  Grafana login page accessible via /grafana/" \
    test "$(curl -s -o /dev/null -w '%{http_code}' -u "${TEST_USER}:${TEST_PASS}" http://localhost:80/grafana/login)" = "200"

# T5: Grafana API healthy
T5_BODY="$(curl -sf -u "${TEST_USER}:${TEST_PASS}" http://localhost:80/grafana/api/health 2>/dev/null || echo "")"
if echo "$T5_BODY" | grep -q '"database"'; then
    ok "T5  Grafana API reports healthy database"
else
    fail "T5  Grafana API reports healthy database (got: $T5_BODY)"
fi

# T6: Sources seeded
run_test "T6  Sources seeded (>= 2)" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T app \
    python -c "
from aoi.db.session import db_session
with db_session() as s:
    from aoi.db.models import Source
    count = s.query(Source).count()
    assert count >= 2, f'Expected >= 2 sources, got {count}'
"

# T7: Static files served
run_test "T7  Static CSS served by nginx" \
    test "$(curl -s -o /dev/null -w '%{http_code}' -u "${TEST_USER}:${TEST_PASS}" http://localhost:80/static/style.css)" = "200"

# T8: Backup produces a dump
info "Running backup.sh..."
BACKUP_COMPOSE_FILE="$COMPOSE_FILE" \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
    exec -T postgres pg_dump -U aoi -d aoi --format=custom > /dev/null 2>&1 || true
# Run backup.sh directly (it uses docker compose exec internally)
BACKUP_OUTPUT="$(./scripts/backup.sh 2>&1)" || true
DUMP_FILE="$(find backups/ -name 'aoi-*.dump' -type f -newer "$ENV_FILE" 2>/dev/null | head -1)"
if [ -n "$DUMP_FILE" ] && [ -s "$DUMP_FILE" ]; then
    ok "T8  Backup produces non-empty dump ($DUMP_FILE)"
else
    fail "T8  Backup produces non-empty dump (no dump file found)"
fi

# T9: Backup is restorable
if [ -n "$DUMP_FILE" ] && [ -s "$DUMP_FILE" ]; then
    # Create a temporary database, restore, check tables exist
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
        exec -T postgres psql -U aoi -d aoi \
        -c "DROP DATABASE IF EXISTS aoi_restore_test;" \
        -c "CREATE DATABASE aoi_restore_test;" >/dev/null 2>&1

    if docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
        exec -T postgres pg_restore -U aoi -d aoi_restore_test --no-owner < "$DUMP_FILE" >/dev/null 2>&1; then

        TABLE_COUNT="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
            exec -T postgres psql -U aoi -d aoi_restore_test -t \
            -c "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" 2>/dev/null | tr -d ' ')"

        if [ -n "$TABLE_COUNT" ] && [ "$TABLE_COUNT" -gt 0 ] 2>/dev/null; then
            ok "T9  Backup restores successfully ($TABLE_COUNT tables)"
        else
            fail "T9  Backup restores successfully (0 tables found)"
        fi
    else
        fail "T9  Backup restores successfully (pg_restore failed)"
    fi

    # Cleanup restore test database
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
        exec -T postgres psql -U aoi -d aoi \
        -c "DROP DATABASE IF EXISTS aoi_restore_test;" >/dev/null 2>&1 || true
else
    fail "T9  Backup restores successfully (skipped — no dump from T8)"
fi

# T10: Scheduler container running
SCHEDULER_STATUS="$(docker inspect --format='{{.State.Status}}' aoi-scheduler 2>/dev/null || echo "not-found")"
if [ "$SCHEDULER_STATUS" = "running" ]; then
    ok "T10 Scheduler container is running"
else
    fail "T10 Scheduler container is running (status: $SCHEDULER_STATUS)"
fi

# T11: Supercronic loaded crontab
SCHEDULER_LOGS="$(docker logs aoi-scheduler 2>&1 || echo "")"
if echo "$SCHEDULER_LOGS" | grep -qi "cron"; then
    ok "T11 Supercronic loaded crontab"
else
    fail "T11 Supercronic loaded crontab (no cron output in logs)"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASSED passed, $FAILED failed (of $((PASSED + FAILED)) tests)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
