#!/bin/sh
# Состояние системы (SQLite) хранится в ветке `state` репозитория — один коммит,
# перезаписываемый force-push'ем, чтобы история не разбухала.
#   state_sync.sh pull  — скачать базу из ветки state (если есть)
#   state_sync.sh push  — сжать и залить текущую базу
set -e
DB="${STATE_DB:-data/marketpulse.db}"
REMOTE="${STATE_REMOTE:-origin}"
case "$1" in
  pull)
    if git fetch -q "$REMOTE" state 2>/dev/null; then
      mkdir -p data
      git show "$REMOTE/state:marketpulse.db.gz" > data/state.db.gz && gunzip -f -c data/state.db.gz > "$DB"
      echo "состояние получено: $(du -h "$DB" | cut -f1)"
    else
      echo "ветки state ещё нет — стартуем с пустой базы"
    fi ;;
  push)
    tmp=$(mktemp -d)
    # консистентный снимок через sqlite backup API (база может быть открыта)
    python3 - "$DB" "$tmp/marketpulse.db" << 'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
src.backup(dst); dst.close(); src.close()
PY
    gzip -9 -f "$tmp/marketpulse.db"
    git -C "$tmp" init -q && git -C "$tmp" checkout -q -b state
    git -C "$tmp" -c user.name=marketpulse-bot -c user.email=bot@marketpulse add marketpulse.db.gz
    git -C "$tmp" -c user.name=marketpulse-bot -c user.email=bot@marketpulse commit -qm "state $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git -C "$tmp" push -q --force "$(git remote get-url "$REMOTE")" state:state
    rm -rf "$tmp"
    echo "состояние отправлено: $(date -u +%H:%M) UTC" ;;
  *) echo "usage: state_sync.sh pull|push"; exit 1 ;;
esac
