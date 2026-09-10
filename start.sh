#!/bin/zsh
# MarketPulse: локальный дашборд. Торговый цикл живёт в облаке (GitHub Actions),
# состояние — в ветке `state` репозитория; дашборд читает свежую копию.
#   ./start.sh          — скачать состояние и поднять дашборд (только чтение)
#   ./start.sh --local  — плюс локальный торговый цикл (только если облако выключено!)
cd "$(dirname "$0")"
mkdir -p data logs

if git fetch -q origin state 2>/dev/null; then
  git show origin/state:marketpulse.db.gz | gunzip -c > data/marketpulse.db.tmp && mv data/marketpulse.db.tmp data/marketpulse.db
  echo "состояние из облака: $(git log -1 --format='%cd' --date=format:'%d.%m %H:%M' origin/state) UTC ($(du -h data/marketpulse.db | cut -f1))"
else
  echo "ветка state недоступна — показываю локальную базу"
fi

pkill -f "uvicorn marketpulse.api.server" 2>/dev/null; sleep 1
PYTHONPATH=src DATABASE_URL="sqlite:///$(pwd)/data/marketpulse.db" nohup .venv/bin/uvicorn marketpulse.api.server:app --app-dir src --port 8737 \
  > logs/server.log 2>&1 &
echo "дашборд: http://localhost:8737   (обновить данные: ./start.sh ещё раз)"

if [[ "$1" == "--local" ]]; then
  if ! pgrep -f "marketpulse.cli run" > /dev/null; then
    PYTHONPATH=src PYTHONUNBUFFERED=1 DATABASE_URL="sqlite:///$(pwd)/data/marketpulse.db" nohup caffeinate -is .venv/bin/python -u -m marketpulse.cli run \
      > logs/night.log 2>&1 &
    echo "локальный цикл запущен — НЕ запускай вместе с облачным: две системы на одном счёте брокера"
  fi
fi
