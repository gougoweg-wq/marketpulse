"""Цикл жизни решения: событие -> признаки -> решение -> исход -> обучение.

Три операции:
  make_decisions()  — по свежим событиям с тикерами создаёт решения;
  record_outcomes() — по прошедшему горизонту фиксирует результат и ДОобучает модель;
  replay_history()  — «разогрев»: проигрывает исторические события по
                      published_at, чтобы модель стартовала не с нуля.

Издержки: 0.05% на сторону (комиссия + проскальзывание) => 0.1% за круг.
Большинство наивных сигналов умирает именно здесь — поэтому издержки
зашиты в realized_return, а не «вспоминаются» потом.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select

from marketpulse.config import settings
from marketpulse.db.models import (
    Article, Decision, DecisionReason, Direction, LogEntry, NewsCluster, PriceBar,
)
from marketpulse.db.session import db_session
from marketpulse.model.features import FEATURE_ORDER, build_features, load_feature_context
from marketpulse.model.learner import OnlineModel

COST_PER_SIDE = 0.0005
ROUND_TRIP_COST = COST_PER_SIDE * 2
FRESH_WINDOW = timedelta(hours=6)   # событие старше — уже не сигнал


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _price_at(s, symbol: str, when: datetime) -> float | None:
    """Первый бар ПОСЛЕ момента when — цена, по которой реально можно войти/выйти."""
    bar = s.execute(
        select(PriceBar).where(PriceBar.symbol == symbol, PriceBar.ts >= _naive(when))
        .order_by(PriceBar.ts.asc()).limit(1)
    ).scalar()
    return bar.close if bar else None


def _last_price(s, symbol: str, when: datetime) -> float | None:
    """Последний бар ДО момента when — текущая известная цена (для размера позиции)."""
    bar = s.execute(
        select(PriceBar).where(PriceBar.symbol == symbol, PriceBar.ts <= _naive(when))
        .order_by(PriceBar.ts.desc()).limit(1)
    ).scalar()
    return bar.close if bar else None


def make_decisions(model: OnlineModel | None = None) -> dict:
    """Решения по свежим событиям, у которых их ещё нет."""
    model = model or OnlineModel.load()
    now = datetime.now(timezone.utc)
    created = 0

    with db_session() as s:
        # рынок закрыт (нет свежих баров) — решений не принимаем: цена входа
        # была бы вчерашней, а исход — фиктивным нулём с издержками
        newest_bar = s.execute(
            select(PriceBar.ts).order_by(PriceBar.ts.desc()).limit(1)
        ).scalar()
        if newest_bar is None or _naive(now) - _naive(newest_bar) > timedelta(hours=3):
            return {"created": 0, "market_closed": True}

        decided = {
            (d.cluster_id, d.symbol) for d in s.execute(
                select(Decision).where(Decision.created_at >= _naive(now - timedelta(days=2)))
            ).scalars()
        }
        # фильтр по тикерам — в Python: у Postgres нет оператора сравнения для json
        fresh = [
            c for c in s.execute(
                select(NewsCluster).where(
                    NewsCluster.first_seen_at >= _naive(now - FRESH_WINDOW),
                )
            ).scalars()
            if c.tickers
        ]

        symbols = sorted({sym for c in fresh for sym in (c.tickers or [])})
        ctx = load_feature_context(s, symbols, now) if symbols else None
        rows: list[dict] = []

        for cluster in fresh:
            for sym in cluster.tickers or []:
                if (cluster.id, sym) in decided:
                    continue
                feats = build_features(cluster, sym, now, ctx)
                if feats is None:
                    continue
                direction, conf, reason = model.decide(feats)
                # последняя известная цена — только для размера позиции;
                # честная цена входа фиксируется в record_outcomes первым баром ПОСЛЕ решения
                bars = ctx["bars"].get(sym) if ctx else None
                entry = bars[-1].close if bars else _last_price(s, sym, now)
                rows.append(dict(
                    cluster_id=cluster.id, symbol=sym, direction=direction,
                    reason=reason, confidence=conf, features=feats,
                    model_version=model.version,
                    horizon_hours=settings.prediction_horizon_hours,
                    entry_price=entry, created_at=_naive(now),
                ))
                created += 1

        if rows:
            s.execute(insert(Decision), rows)  # один пакет вместо сотен INSERT
        if created:
            s.add(LogEntry(
                component="model",
                message=f"принято решений: {created} (модель {model.version})",
            ))
    return {"created": created}


def record_outcomes(model: OnlineModel | None = None) -> dict:
    """Фиксирует исходы решений с истёкшим горизонтом и дообучает модель.

    Метка для обучения — направление рынка (вырос/упал), поэтому учимся
    на ВСЕХ решениях, включая flat: модель предсказывает рынок, а не
    собственную награду.
    """
    model = model or OnlineModel.load()
    now = datetime.now(timezone.utc)
    recorded = 0

    with db_session() as s:
        pending = s.execute(
            select(Decision).where(Decision.outcome_recorded_at.is_(None))
        ).scalars().all()

        for d in pending:
            horizon_end = _naive(d.created_at) + timedelta(hours=d.horizon_hours)
            if _naive(now) < horizon_end:
                continue
            # честный вход: первый бар ПОСЛЕ решения (никакого взгляда назад)
            true_entry = _price_at(s, d.symbol, _naive(d.created_at))
            exit_price = _price_at(s, d.symbol, horizon_end)
            if exit_price is None and _naive(now) - horizon_end > timedelta(hours=2):
                # горизонт пришёлся на закрытый рынок — выходим по последнему
                # бару до горизонта (эквивалент выхода по закрытию сессии)
                exit_price = _last_price(s, d.symbol, horizon_end)
            if true_entry is None or exit_price is None:
                continue  # цены ещё не подгрузились — попробуем в следующий раз
            d.entry_price = true_entry

            market_ret = exit_price / d.entry_price - 1
            if d.direction == Direction.long:
                realized = market_ret - ROUND_TRIP_COST
            elif d.direction == Direction.short:
                realized = -market_ret - ROUND_TRIP_COST
            else:
                realized = 0.0

            d.exit_price = exit_price
            d.realized_return = realized
            d.outcome_recorded_at = _naive(now)
            # copy-решения имеют другой набор признаков — исход фиксируем,
            # но в модель направления рынка их не скармливаем
            feats = d.features or {}
            if (
                market_ret != 0  # нулевое движение = закрытый рынок, не сигнал
                and d.reason != DecisionReason.copy
                and all(k in feats for k in FEATURE_ORDER)
            ):
                model.learn_one(feats, went_up=market_ret > 0)
            recorded += 1

        if recorded:
            s.add(LogEntry(
                component="model",
                message=f"зафиксировано исходов: {recorded}, модель -> {model.version}",
            ))

    model.save()
    return {"recorded": recorded}


def replay_history() -> dict:
    """Разогрев модели на исторических событиях.

    Живой цикл использует fetched_at (когда МЫ увидели новость). Для
    истории её нет — берём published_at. Это компромисс разогрева,
    и поэтому реплей-решения помечаются reason=exploration и не
    смешиваются с боевой статистикой.
    """
    model = OnlineModel.load()
    trained = 0
    skipped = 0

    with db_session() as s:
        rows = s.execute(
            select(NewsCluster, Article.published_at)
            .join(Article, Article.cluster_id == NewsCluster.id)
            .where(Article.published_at.isnot(None))
            .order_by(Article.published_at.asc())
        ).all()

        seen: set[tuple[int, str]] = set()
        for cluster, published_at in rows:
            if not cluster.tickers:
                continue
            event_time = _naive(published_at)
            for sym in cluster.tickers or []:
                if (cluster.id, sym) in seen:
                    continue
                seen.add((cluster.id, sym))

                feats = build_features(cluster, sym, event_time)
                if feats is None:
                    skipped += 1
                    continue
                entry = _price_at(s, sym, event_time)
                exit_p = _price_at(
                    s, sym, event_time + timedelta(hours=settings.prediction_horizon_hours))
                if entry is None or exit_p is None or entry == exit_p:
                    skipped += 1
                    continue
                model.learn_one(feats, went_up=exit_p > entry)
                trained += 1

        s.add(LogEntry(
            component="model",
            message=f"разогрев: обучена на {trained} исторических событиях "
                    f"(пропущено {skipped}), модель {model.version}",
        ))

    model.save()
    return {"trained": trained, "skipped": skipped}
