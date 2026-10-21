#!/usr/bin/env bash
# ─── AOI Backup Script ───────────────────────────────────────────────────────
# Creates a timestamped PostgreSQL dump. It does NOT back up the SeaweedFS volume
# (the raw evidence); M9 replaces this script with restic off-site backups.
#
# Usage:
#   ./scripts/backup.sh                    # local backup to backups/
#   ./scripts/backup.sh --upload s3://...  # backup + upload to external S3
#
# Cron (on VPS):
#   0 3 * * * cd /opt/aoi && ./scripts/backup.sh >> logs/backup.log 2>&1
#
# Restore:
#   docker compose -f docker-compose.prod.yml exec -T postgres \
#     pg_restore -U aoi -d aoi --clean --if-exists < backups/aoi-YYYYMMDD-HHMMSS.dump

set -euo pipefail

STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="backups"
COMPOSE_FILE="docker-compose.prod.yml"

# Use prod compose if it exists, otherwise dev
if [ ! -f "$COMPOSE_FILE" ]; then
    COMPOSE_FILE="docker-compose.yml"
fi

mkdir -p "$BACKUP_DIR"

echo "[$(date -Iseconds)] Starting backup..."

# ── PostgreSQL dump ──────────────────────────────────────────────────────────
PG_DUMP="$BACKUP_DIR/aoi-${STAMP}.dump"
PG_DUMP_TMP="${PG_DUMP}.tmp"
docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -U aoi -d aoi --format=custom \
    > "$PG_DUMP_TMP"

# Verify the dump is non-empty before promoting it
if [ ! -s "$PG_DUMP_TMP" ]; then
    rm -f "$PG_DUMP_TMP"
    echo "[$(date -Iseconds)] ERROR: pg_dump produced an empty file" >&2
    exit 1
fi
mv "$PG_DUMP_TMP" "$PG_DUMP"
echo "[$(date -Iseconds)] PostgreSQL dump: $PG_DUMP ($(du -h "$PG_DUMP" | cut -f1))"

# ── Retention: delete backups older than 30 days ─────────────────────────────
DELETED=$(find "$BACKUP_DIR" -name "aoi-*.dump" -mtime +30 -delete -print | wc -l)
if [ "$DELETED" -gt 0 ]; then
    echo "[$(date -Iseconds)] Cleaned $DELETED backups older than 30 days"
fi

echo "[$(date -Iseconds)] Backup complete."
