"""Полная копия облачной базы в локальный SQLite — страховка от блокировок тарифа.

Запуск: PYTHONPATH=src .venv/bin/python scripts/backup_to_sqlite.py [путь.db]
Копирует все таблицы (источники, статьи, события, котировки, решения, сделки,
инсайдеры, журнал, веса моделей). Локальную копию можно подставить как
DATABASE_URL=sqlite:///... и продолжить работу без облака.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, insert, select

from marketpulse.db.models import Base
from marketpulse.db.session import engine as cloud

target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/backup.db")
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists():
    target.unlink()
local = create_engine(f"sqlite:///{target}")
Base.metadata.create_all(local)

BATCH = 2000
with cloud.connect() as src, local.begin() as dst:
    for table in Base.metadata.sorted_tables:
        total = 0
        result = src.execution_options(stream_results=True).execute(select(table))
        while True:
            rows = result.fetchmany(BATCH)
            if not rows:
                break
            dst.execute(insert(table), [dict(r._mapping) for r in rows])
            total += len(rows)
        print(f"{table.name:18s} {total:8d} строк")
print(f"копия: {target} ({target.stat().st_size / 1e6:.1f} МБ)")
