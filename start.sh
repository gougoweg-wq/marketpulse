#!/bin/zsh
# MarketPulse: запуск всего одной командой (дашборд + ночной цикл)
cd "$(dirname "$0")"

# дашборд
if ! pgrep -f "uvicorn marketpulse.api.server" > /dev/null; then
  PYTHONPATH=src nohup .venv/bin/uvicorn marketpulse.api.server:app --app-dir src --port 8737 \
    > logs/server.log 2>&1 &
  echo "дашборд запущен: http://localhost:8737"
else
  echo "дашборд уже работает: http://localhost:8737"
fi

# торговый цикл (не даёт маку заснуть, пока работает)
if ! pgrep -f "marketpulse.cli run" > /dev/null; then
  PYTHONPATH=src PYTHONUNBUFFERED=1 nohup caffeinate -is .venv/bin/python -u -m marketpulse.cli run \
    > logs/night.log 2>&1 &
  echo "цикл запущен, PID: $!"
else
  echo "цикл уже работает, PID: $(pgrep -f 'marketpulse.cli run' | head -1)"
fi
