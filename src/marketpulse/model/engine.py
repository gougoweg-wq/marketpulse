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

from sqlalchemy import func, insert, select

from marketpulse.config import settings
from marketpulse.db.models import (
    Article, Decision, DecisionReason, Direction, LogEntry, NewsCluster, PriceBar,
)
from marketpulse.db.session import db_session
from marketpulse.model.features import FEATURE_ORDER, build_features, load_feature_context
from marketpulse.model.learner import OnlineModel

COST_PER_SIDE = 0.0005
ROUND_TRIP_COST = COST_PER_SIDE * 2
FRESH_WINDOW = timedelta(hours=16)      # событие старше — уже не сигнал (ночь до открытия — 15 ч)
PUBLISHED_MAX_AGE = timedelta(hours=24)  # по времени публикации: бэклог лент — не новости
MARKET_STALE = timedelta(minutes=75)     # нет бара свежее — рынок закрыт
VOID_AFTER = timedelta(days=3)           # решение без цен дольше — аннулируем


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _bar_at(s, symbol: str, when: datetime):
    """Первый бар ПОСЛЕ момента when — по нему реально можно войти/выйти."""
    return s.execute(
        select(PriceBar).where(PriceBar.symbol == symbol, PriceBar.ts >= _naive(when))
        .order_by(PriceBar.ts.asc()).limit(1)
    ).scalar()


def _last_bar(s, symbol: str, when: datetime):
    """Последний бар ДО момента when."""
    return s.execute(
        select(PriceBar).where(PriceBar.symbol == symbol, PriceBar.ts <= _naive(when))
        .order_by(PriceBar.ts.desc()).limit(1)
    ).scalar()


def _price_at(s, symbol: str, when: datetime) -> float | None:
    bar = _bar_at(s, symbol, when)
    return bar.close if bar else None


def _last_price(s, symbol: str, when: datetime) -> float | None:
    bar = _last_bar(s, symbol, when)
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
        if newest_bar is None or _naive(now) - _naive(newest_bar) > MARKET_STALE:
            return {"created": 0, "market_closed": True}

        decided = {
            (d.cluster_id, d.symbol) for d in s.execute(
                select(Decision).where(Decision.created_at >= _naive(now - timedelta(days=2)))
            ).scalars()
        }
        # фильтр по тикерам — в Python: у Postgres нет оператора сравнения для json
        window = _naive(now - FRESH_WINDOW)
        first_published = dict(s.execute(
            select(Article.cluster_id, func.min(Article.published_at))
            .join(NewsCluster, NewsCluster.id == Article.cluster_id)
            .where(NewsCluster.first_seen_at >= window)
            .group_by(Article.cluster_id)
        ).all())
        fresh = []
        for c in s.execute(
            select(NewsCluster).where(NewsCluster.first_seen_at >= window)
        ).scalars():
            if not c.tickers:
                continue
            pub = first_published.get(c.id)
            if pub is not None and _naive(now) - _naive(pub) > PUBLISHED_MAX_AGE:
                continue  # старая публикация, только что попавшая в ленту
            fresh.append(c)

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
    voided = 0

    with db_session() as s:
        pending = s.execute(
            select(Decision).where(Decision.outcome_recorded_at.is_(None))
        ).scalars().all()
        due = [d for d in pending
               if _naive(now) >= _naive(d.created_at) + timedelta(hours=d.horizon_hours)]
        if not due:
            return {"recorded": 0, "voided": 0}

        # бары всех нужных тикеров одним запросом; поиск входа/выхода — в памяти
        from bisect import bisect_left, bisect_right

        symbols = sorted({d.symbol for d in due})
        earliest = min(_naive(d.created_at) for d in due) - timedelta(days=3)
        bars_by_symbol: dict[str, list] = {}
        for b in s.execute(
            select(PriceBar).where(PriceBar.symbol.in_(symbols), PriceBar.ts >= earliest)
            .order_by(PriceBar.symbol, PriceBar.ts)
        ).scalars():
            bars_by_symbol.setdefault(b.symbol, []).append(b)
        ts_index = {sym: [_naive(b.ts) for b in bars] for sym, bars in bars_by_symbol.items()}

        def bar_at(sym: str, when: datetime):
            """Первый бар >= when."""
            i = bisect_left(ts_index.get(sym, []), when)
            bars = bars_by_symbol.get(sym, [])
            return bars[i] if i < len(bars) else None

        def last_bar(sym: str, when: datetime):
            """Последний бар <= when."""
            i = bisect_right(ts_index.get(sym, []), when) - 1
            bars = bars_by_symbol.get(sym, [])
            return bars[i] if i >= 0 else None

        for d in due:
            horizon_end = _naive(d.created_at) + timedelta(hours=d.horizon_hours)
            # честный вход: первый бар ПОСЛЕ решения (никакого взгляда назад)
            entry_bar = bar_at(d.symbol, _naive(d.created_at))
            exit_bar = bar_at(d.symbol, horizon_end)
            if exit_bar is None and _naive(now) - horizon_end > timedelta(hours=2):
                # горизонт пришёлся на закрытый рынок — выходим по последнему
                # бару до горизонта (эквивалент выхода по закрытию сессии)
                exit_bar = last_bar(d.symbol, horizon_end)
            if entry_bar is None or exit_bar is None:
                if _naive(now) - horizon_end > VOID_AFTER:
                    # цен так и не появилось (тикер выбыл) — аннулируем, не держим экспозицию
                    d.realized_return = 0.0
                    d.outcome_recorded_at = _naive(now)
                    voided += 1
                continue  # цены ещё не подгрузились — попробуем в следующий раз
            if _naive(exit_bar.ts) <= _naive(entry_bar.ts):
                # вход и выход — один бар: сделки по сути не было, аннулируем без издержек
                d.entry_price = entry_bar.close
                d.exit_price = entry_bar.close
                d.realized_return = 0.0
                d.outcome_recorded_at = _naive(now)
                voided += 1
                continue
            d.entry_price = entry_bar.close
            exit_price = exit_bar.close

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

        if recorded or voided:
            s.add(LogEntry(
                component="model",
                message=f"зафиксировано исходов: {recorded}, аннулировано: {voided}, "
                        f"модель -> {model.version}",
            ))
        if recorded:
            model.save(s)  # в одной транзакции с исходами: либо всё, либо ничего

    return {"recorded": recorded, "voided": voided}


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
