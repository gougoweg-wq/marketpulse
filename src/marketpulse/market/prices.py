"""Загрузка котировок через yfinance.

Часовые бары за последние N дней по всему вотчлисту. Повторный запуск
дозагружает только новое (upsert по (symbol, interval, ts)).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from sqlalchemy import insert, select

from marketpulse.config import settings
from marketpulse.db.models import LogEntry, PriceBar
from marketpulse.db.session import db_session

log = logging.getLogger("market")

HISTORY_DAYS = 60  # для 1h-баров yfinance отдаёт максимум ~730 дней


def fetch_prices(symbols: list[str] | None = None) -> dict:
    symbols = symbols or settings.watchlist
    interval = settings.price_bar_interval
    inserted = 0
    failed: list[str] = []

    # один батч-запрос на все тикеры сразу
    data = yf.download(
        tickers=" ".join(symbols),
        period=f"{HISTORY_DAYS}d",
        interval=interval,
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )

    with db_session() as s:
        for sym in symbols:
            try:
                df = data[sym] if len(symbols) > 1 else data
            except KeyError:
                failed.append(sym)
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                failed.append(sym)
                continue

            existing = {
                ts for ts in s.execute(
                    select(PriceBar.ts).where(
                        PriceBar.symbol == sym, PriceBar.interval == interval
                    )
                ).scalars()
            }
            # SQLite отдаёт naive datetime — нормализуем для сравнения
            existing = {t.replace(tzinfo=None) for t in existing}

            batch = []
            for ts, row in df.iterrows():
                ts_utc = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
                key = ts_utc.tz_localize(None).to_pydatetime()
                if key in existing:
                    continue
                batch.append(dict(
                    symbol=sym, interval=interval, ts=ts_utc.to_pydatetime(),
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=float(row["Volume"] or 0),
                ))
            if batch:
                # одним пакетом: построчная вставка в удалённый Postgres — минуты
                s.execute(insert(PriceBar), batch)
                inserted += len(batch)

        s.add(LogEntry(
            component="market",
            message=f"котировки: +{inserted} баров, ошибок: {len(failed)}",
            payload={"inserted": inserted, "failed": failed},
        ))

    return {"inserted": inserted, "failed": failed}


def latest_price(symbol: str) -> float | None:
    with db_session() as s:
        bar = s.execute(
            select(PriceBar).where(PriceBar.symbol == symbol)
            .order_by(PriceBar.ts.desc()).limit(1)
        ).scalar()
        return bar.close if bar else None


def price_at(symbol: str, when: datetime) -> float | None:
    """Ближайший бар ПОСЛЕ момента when — цена, по которой реально можно было войти."""
    with db_session() as s:
        bar = s.execute(
            select(PriceBar).where(
                PriceBar.symbol == symbol,
                PriceBar.ts >= when.replace(tzinfo=None) if when.tzinfo else when,
            ).order_by(PriceBar.ts.asc()).limit(1)
        ).scalar()
        return bar.close if bar else None
