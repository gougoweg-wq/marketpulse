#!/bin/zsh
# Ручная сделка через очередь в репозитории (исполняет облачный тик).
#   ./manual.sh NVDA long 72 10000        # тикер, long|short, часы, сумма$, [плечо для крипты]
set -e
cd "$(dirname "$0")"
[ $# -ge 2 ] || { echo "usage: ./manual.sh SYMBOL long|short [часы] [сумма$] [плечо]"; exit 1; }
git pull -q --rebase origin main
mkdir -p queue
echo "$(date -u +%Y%m%dT%H%M%S) $*" >> queue/manual.txt
git add queue/manual.txt && git commit -qm "queue: manual $*" && git push -q origin main
echo "в очереди: $* — исполнится на ближайшем тике облака (до 15 мин)"
