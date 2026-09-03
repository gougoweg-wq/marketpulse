"""Построение признаков для пары (событие, тикер).

Каждое решение принимается по снимку признаков на момент события.
Никаких данных из будущего: все окна смотрят строго назад.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from marketpulse.db.models import NewsCluster, PriceBar
from marketpulse.db.session import db_session

# мусорные приписки в телеграм-постах, искажающие тональность
_JUNK_RE = re.compile(
    r"НАСТОЯЩИЙ МАТЕРИАЛ \(ИНФОРМАЦИЯ\).*?ИНОСТРАННОГО АГЕНТА[^.]*\.?",
    re.I | re.S,
)


def clean_text(text: str) -> str:
    return _JUNK_RE.sub(" ", text)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def load_feature_context(s, symbols: list[str], at: datetime) -> dict:
    """Всё нужное для признаков за два запроса: бары по тикерам и события за 7 дней."""
    at_n = _naive(at)
    bars_by_symbol: dict[str, list] = {}
    for b in s.execute(
        select(PriceBar).where(
            PriceBar.symbol.in_(symbols),
            PriceBar.ts >= at_n - timedelta(days=9),
            PriceBar.ts < at_n,
        ).order_by(PriceBar.ts.asc())
    ).scalars():
        bars_by_symbol.setdefault(b.symbol, []).append(b)
    clusters = s.execute(
        select(NewsCluster).where(
            NewsCluster.first_seen_at >= at_n - timedelta(days=7),
            NewsCluster.first_seen_at < at_n,
        )
    ).scalars().all()
    return {"bars": bars_by_symbol, "clusters": clusters}


def build_features(cluster: NewsCluster, symbol: str, at: datetime, ctx: dict | None = None) -> dict | None:
    """Снимок признаков. None — если по тикеру нет ценовой истории.

    ctx — предзагруженный контекст (см. load_feature_context); без него
    функция ходит в базу сама (режим одиночного вызова / реплея).
    """
    at_n = _naive(at)

    if ctx is None:
        with db_session() as s:
            ctx = load_feature_context(s, [symbol], at)

    bars = [b for b in ctx["bars"].get(symbol, []) if b.ts < at_n][-24 * 7:]
    if len(bars) < 30:
        return None
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]

    # --- рыночные признаки (только прошлое) ---
    ret_24h = closes[-1] / closes[-24] - 1 if len(closes) >= 24 else 0.0
    ret_5d = closes[-1] / closes[0] - 1
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean_r = sum(rets) / len(rets)
    volatility = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / len(rets))
    vol_avg = sum(vols) / max(len(vols), 1)
    volume_spike = (vols[-1] / vol_avg - 1) if vol_avg > 0 else 0.0

    # --- новостной фон: сколько событий с этим тикером за 24ч и за 7д ---
    day_ago = at_n - timedelta(hours=24)
    week_ago = at_n - timedelta(days=7)
    buzz_24h = 0
    buzz_7d = 0
    sent_sum_24h = 0.0
    for c in ctx["clusters"]:
        fs = _naive(c.first_seen_at)
        if fs < week_ago or fs >= at_n or symbol not in (c.tickers or []):
            continue
        buzz_7d += 1
        if fs >= day_ago:
            buzz_24h += 1
            sent_sum_24h += c.sentiment or 0.0

    buzz_baseline = buzz_7d / 7.0
    buzz_ratio = buzz_24h / buzz_baseline if buzz_baseline > 0.5 else float(buzz_24h)
    crowd_sent = sent_sum_24h / buzz_24h if buzz_24h else 0.0

    return {
        # событие
        "sentiment": cluster.sentiment or 0.0,
        "n_sources": min(cluster.n_sources, 20) / 20.0,   # охват, нормирован
        "n_articles": min(cluster.n_articles, 40) / 40.0,
        # фон по тикеру
        "buzz_ratio": min(buzz_ratio, 10.0) / 10.0,        # всплеск обсуждений
        "crowd_sentiment": crowd_sent,                     # настроение толпы за 24ч
        "crowd_extreme": 1.0 if abs(crowd_sent) > 0.5 and buzz_24h >= 5 else 0.0,
        # рынок
        "ret_24h": max(-0.2, min(0.2, ret_24h)) / 0.2,
        "ret_5d": max(-0.4, min(0.4, ret_5d)) / 0.4,
        "volatility": min(volatility, 0.05) / 0.05,
        "volume_spike": max(-1.0, min(5.0, volume_spike)) / 5.0,
    }


FEATURE_ORDER = [
    "sentiment", "n_sources", "n_articles", "buzz_ratio", "crowd_sentiment",
    "crowd_extreme", "ret_24h", "ret_5d", "volatility", "volume_spike",
]


def to_vector(features: dict) -> list[float]:
    return [features[k] for k in FEATURE_ORDER]
