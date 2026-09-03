#!/bin/zsh
# MarketPulse: локальный дашборд (торговый цикл живёт в облаке GitHub Actions).
# ./start.sh          — только дашборд
# ./start.sh --local  — дашборд + локальный торговый цикл (если облако выключено)
cd "$(dirname "$0")"

if ! pgrep -f "uvicorn marketpulse.api.server" > /dev/null; then
  PYTHONPATH=src nohup .venv/bin/uvicorn marketpulse.api.server:app --app-dir src --port 8737 \
    > logs/server.log 2>&1 &
  echo "дашборд: http://localhost:8737"
else
  echo "дашборд уже работает: http://localhost:8737"
fi

if [[ "$1" == "--local" ]]; then
  if ! pgrep -f "marketpulse.cli run" > /dev/null; then
    PYTHONPATH=src PYTHONUNBUFFERED=1 nohup caffeinate -is .venv/bin/python -u -m marketpulse.cli run \
      > logs/night.log 2>&1 &
    echo "локальный цикл запущен (не запускай вместе с облачным — будут дубли ордеров)"
  fi
fi
