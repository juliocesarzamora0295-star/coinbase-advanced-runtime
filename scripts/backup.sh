#!/bin/bash
# Backup state and logs — run daily via cron
set -euo pipefail

BACKUP_DIR="${1:-backup}"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p "$BACKUP_DIR"

if [ -d "state" ]; then
    tar czf "$BACKUP_DIR/state_${DATE}.tar.gz" state/
    echo "State backed up: $BACKUP_DIR/state_${DATE}.tar.gz"
fi

if [ -d "logs" ]; then
    tar czf "$BACKUP_DIR/logs_${DATE}.tar.gz" logs/
    echo "Logs backed up: $BACKUP_DIR/logs_${DATE}.tar.gz"
fi

# Retain last 30 days
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +30 -delete 2>/dev/null || true
echo "Backup complete."
