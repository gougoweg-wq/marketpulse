"""Загрузка котировок через yfinance.

Часовые бары по всему вотчлисту. Вставка — upsert по (symbol, interval, ts):
yfinance отдаёт текущий незавершённый бар, и его нужно ОБНОВЛЯТЬ на каждом
тике, иначе в базе навсегда останется цена/объём первых минут часа.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from sqlalchemy import func, select

from marketpulse.config import settings
from marketpulse.db.models import LogEntry, PriceBar
from marketpulse.db.session import db_session, engine

log = logging.getLogger("market")

HISTORY_DAYS = 60  # для 1h-баров yfinance отдаёт максимум ~730 дней
BAR_COLUMNS = ("open", "high", "low", "close", "volume")


def _upsert_stmt():
    """INSERT ... ON CONFLICT (symbol, interval, ts) DO UPDATE — для обоих диалектов."""
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(PriceBar)
    return stmt.on_conflict_do_update(
        index_elements=["symbol", "interval", "ts"],
        set_={c: getattr(stmt.excluded, c) for c in BAR_COLUMNS},
    )


def fetch_prices(symbols: list[str] | None = None) -> dict:
    symbols = symbols or settings.watchlist
    interval = settings.price_bar_interval
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

    rows: list[dict] = []
    with db_session() as s:
        # что уже есть: по каждому тикеру последний бар — обновляем только хвост
        # (последние 2 дня), иначе каждый тик гонял бы через океан 60 дней истории
        last_ts = dict(s.execute(
            select(PriceBar.symbol, func.max(PriceBar.ts))
            .where(PriceBar.interval == interval).group_by(PriceBar.symbol)
        ).all())

        for sym in symbols:
            try:
                # с group_by="ticker" колонки всегда двухуровневые (тикер, поле)
                df = data[sym] if isinstance(data.columns, pd.MultiIndex) else data
            except KeyError:
                failed.append(sym)
                continue
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                failed.append(sym)
                continue

            floor = None
            if last_ts.get(sym) is not None:
                floor = last_ts[sym].replace(tzinfo=None) - timedelta(days=2)

            for ts, row in df.iterrows():
                ts_utc = ts.tz_convert("UTC") if ts.tzinfo else ts.tz_localize("UTC")
                if floor is not None and ts_utc.tz_localize(None).to_pydatetime() < floor:
                    continue
                vol = row["Volume"]
                rows.append(dict(
                    symbol=sym, interval=interval, ts=ts_utc.to_pydatetime(),
                    open=float(row["Open"]), high=float(row["High"]),
                    low=float(row["Low"]), close=float(row["Close"]),
                    volume=0.0 if pd.isna(vol) else float(vol),  # NaN не должен попасть в базу
                ))

        if rows:
            s.execute(_upsert_stmt(), rows)  # один пакет, upsert

        s.add(LogEntry(
            component="market",
            message=f"котировки: обновлено {len(rows)} баров, ошибок: {len(failed)}",
            payload={"upserted": len(rows), "failed": failed},
        ))

    return {"inserted": len(rows), "failed": failed}
