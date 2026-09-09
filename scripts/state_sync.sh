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
    # коммит через plumbing в ТЕКУЩЕМ репозитории: у него есть учётные данные
    # (actions/checkout кладёт токен в .git/config); отдельный временный репозиторий их не видит
    blob=$(git hash-object -w "$tmp/marketpulse.db.gz")
    tree=$(printf '100644 blob %s\tmarketpulse.db.gz\n' "$blob" | git mktree)
    commit=$(GIT_AUTHOR_NAME=marketpulse-bot GIT_AUTHOR_EMAIL=bot@marketpulse \
             GIT_COMMITTER_NAME=marketpulse-bot GIT_COMMITTER_EMAIL=bot@marketpulse \
             git commit-tree "$tree" -m "state $(date -u +%Y-%m-%dT%H:%M:%SZ)")
    git push -q --force "$REMOTE" "$commit:refs/heads/state"
    rm -rf "$tmp"
    echo "состояние отправлено: $(date -u +%H:%M) UTC" ;;
  *) echo "usage: state_sync.sh pull|push"; exit 1 ;;
esac
