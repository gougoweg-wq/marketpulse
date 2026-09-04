"""Исполнение решений на демо-счёте.

Два режима:
  - внутренний симулятор (по умолчанию): виртуальные $100k, сделки
    закрываются по горизонту решения, PnL считается из realized_return;
  - Alpaca paper (если заданы ключи): ордера дублируются на реальный
    демо-счёт брокера и закрываются там обратными ордерами.
    Реальная торговля в коде отсутствует (paper=True жёстко).

Порядок в execute_new_decisions намеренно двухфазный: сначала сделки
фиксируются в базе (INSERT ... ON CONFLICT по decision_id) и коммитятся,
и только потом уходят ордера брокеру. Иначе откат транзакции после отправки
оставлял бы у брокера «сироту» и слал бы дубль на следующем тике.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from marketpulse.config import settings
from marketpulse.db.models import (
    Decision, DecisionReason, Direction, LogEntry, PriceBar, Trade, TradeStatus,
)
from marketpulse.db.session import db_session, engine

STARTING_EQUITY = 100_000.0
DECISION_MAX_AGE = timedelta(minutes=20)   # решение старше одного тика не исполняем
MARKET_STALE = timedelta(minutes=75)       # нет бара свежее — рынок закрыт


def account_equity(s) -> float:
    """Текущий капитал симулятора = старт + сумма PnL закрытых сделок."""
    pnl = s.execute(
        select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(Trade.status == TradeStatus.closed)
    ).scalar()
    return STARTING_EQUITY + float(pnl or 0.0)


def open_exposure(s) -> float:
    exp = s.execute(
        select(func.coalesce(func.sum(Trade.notional), 0.0)).where(Trade.status == TradeStatus.filled)
    ).scalar()
    return float(exp or 0.0)


def _alpaca_client():
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        return None
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(
            settings.alpaca_api_key, settings.alpaca_secret_key,
            paper=True,  # жёстко: только демо
        )
    except Exception:
        return None


def _market_open(s, client) -> bool:
    """Часы торгов: у брокера — точно; без брокера — по свежести последнего бара."""
    if client is not None:
        try:
            return bool(client.get_clock().is_open)
        except Exception:
            pass
    newest = s.execute(select(func.max(PriceBar.ts))).scalar()
    if newest is None:
        return False
    newest = newest.replace(tzinfo=None)
    return datetime.now(timezone.utc).replace(tzinfo=None) - newest <= MARKET_STALE


def _submit_alpaca(client, trade: Trade) -> tuple[str | None, str | None]:
    """(id ордера, ошибка). Шорт — только целыми акциями: дробный шорт брокер не принимает."""
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if trade.direction == Direction.long:
            req = MarketOrderRequest(symbol=trade.symbol, notional=round(trade.notional, 2),
                                     side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        else:
            qty = int(trade.notional / trade.fill_price)
            if qty < 1:
                return None, "шорт меньше одной акции — только симулятор"
            req = MarketOrderRequest(symbol=trade.symbol, qty=qty,
                                     side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = client.submit_order(req)
        return str(order.id), None
    except Exception as exc:  # noqa: BLE001 — ошибка брокера не должна валить тик
        return None, f"{type(exc).__name__}: {str(exc)[:160]}"


def _close_alpaca(client, trade: Trade) -> str | None:
    """Обратный ордер на демо-счёте; None — если не отправлялся/ошибка."""
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        if trade.direction == Direction.long:
            req = MarketOrderRequest(symbol=trade.symbol, notional=round(trade.notional, 2),
                                     side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        else:
            qty = int(trade.notional / trade.fill_price)
            if qty < 1:
                return None
            req = MarketOrderRequest(symbol=trade.symbol, qty=qty,
                                     side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        return str(client.submit_order(req).id)
    except Exception:  # noqa: BLE001
        return None


def _trade_insert_stmt():
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    return dialect_insert(Trade).on_conflict_do_nothing(index_elements=["decision_id"])


def execute_new_decisions() -> dict:
    """Открывает сделки по свежим решениям long/short без сделки."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    opened = 0
    skipped_risk = 0
    alpaca = _alpaca_client()

    # ---------- фаза 1: зафиксировать сделки в базе ----------
    with db_session() as s:
        if not _market_open(s, alpaca):
            return {"opened": 0, "skipped_risk": 0, "market_closed": True}

        equity = account_equity(s)
        exposure = open_exposure(s)
        traded = set(s.execute(select(Trade.decision_id)).scalars())

        pending = s.execute(
            select(Decision).where(
                Decision.direction != Direction.flat,
                Decision.outcome_recorded_at.is_(None),
                Decision.entry_price.isnot(None),
                Decision.created_at >= now - DECISION_MAX_AGE,  # не исполнять с известным исходом
            )
        ).scalars().all()

        # экспозиция по тикеру среди открытых — лимит концентрации
        symbol_exposure: dict[str, float] = dict(s.execute(
            select(Trade.symbol, func.sum(Trade.notional))
            .where(Trade.status == TradeStatus.filled).group_by(Trade.symbol)
        ).all())

        rows: list[dict] = []
        for d in pending:
            if d.id in traded:
                continue
            base = equity * settings.max_position_pct
            conf_frac = min(1.0, max(0.2, (d.confidence - 0.5) / 0.15))
            notional = base * conf_frac
            if d.reason == DecisionReason.exploration:
                notional *= 0.25
            if exposure + notional > equity * settings.max_gross_exposure:
                skipped_risk += 1
                continue
            # не более 2× размера позиции в одном тикере
            if symbol_exposure.get(d.symbol, 0.0) + notional > equity * settings.max_position_pct * 2:
                skipped_risk += 1
                continue
            symbol_exposure[d.symbol] = symbol_exposure.get(d.symbol, 0.0) + notional
            exposure += notional
            rows.append(dict(
                decision_id=d.id, symbol=d.symbol, direction=d.direction,
                qty=notional / d.entry_price, notional=notional,
                status=TradeStatus.submitted, submitted_at=now, fill_price=d.entry_price,
            ))
        if rows:
            s.execute(_trade_insert_stmt(), rows)

    # ---------- фаза 2: отправить ордера и пометить исполненными ----------
    with db_session() as s:
        submitted = s.execute(
            select(Trade).where(Trade.status == TradeStatus.submitted)
        ).scalars().all()
        errors: list[str] = []
        for t in submitted:
            if alpaca is not None:
                order_id, err = _submit_alpaca(alpaca, t)
                t.broker_order_id = order_id
                if err:
                    errors.append(f"{t.symbol}: {err}")
            t.status = TradeStatus.filled
            t.filled_at = now
            opened += 1

        if opened or skipped_risk:
            s.add(LogEntry(
                component="trading",
                message=f"открыто сделок: {opened}, отклонено риск-лимитом: {skipped_risk}, "
                        f"капитал=${equity:,.0f}, экспозиция=${exposure:,.0f}"
                        + (f"; брокер отклонил {len(errors)}" if errors else ""),
                payload={"opened": opened, "skipped": skipped_risk, "equity": equity,
                         "broker_errors": errors[:10]},
                level="warn" if errors else "info",
            ))
    return {"opened": opened, "skipped_risk": skipped_risk}


def close_expired_trades() -> dict:
    """Закрывает сделки, чьи решения получили исход; у брокера — обратным ордером."""
    closed = 0
    total_pnl = 0.0
    alpaca = _alpaca_client()

    with db_session() as s:
        pairs = s.execute(
            select(Trade, Decision)
            .join(Decision, Decision.id == Trade.decision_id)
            .where(Trade.status == TradeStatus.filled, Decision.realized_return.isnot(None))
        ).all()
        for t, d in pairs:
            t.status = TradeStatus.closed
            # время закрытия = конец горизонта решения, а не момент запуска скрипта
            t.closed_at = d.created_at.replace(tzinfo=None) + timedelta(hours=d.horizon_hours)
            t.close_price = d.exit_price
            t.pnl = t.notional * d.realized_return
            total_pnl += t.pnl
            closed += 1
            if alpaca is not None and t.broker_order_id:
                _close_alpaca(alpaca, t)  # позиция у брокера не должна жить вечно

        if closed:
            s.add(LogEntry(
                component="trading",
                message=f"закрыто сделок: {closed}, PnL за партию: ${total_pnl:+,.2f}",
                payload={"closed": closed, "pnl": total_pnl},
            ))

        # сверка с брокером: позиция, у которой нет открытой сделки в базе, —
        # сирота (осталась от сбоя или старой версии), закрываем целиком
        orphans = _reconcile_broker(s, alpaca)
        if orphans:
            s.add(LogEntry(
                component="trading", level="warn",
                message=f"сверка с брокером: закрыто позиций-сирот: {len(orphans)} ({', '.join(orphans)})",
            ))
    return {"closed": closed, "pnl": total_pnl, "orphans_closed": len(orphans) if alpaca else 0}


def _reconcile_broker(s, client) -> list[str]:
    if client is None:
        return []
    try:
        positions = client.get_all_positions()
    except Exception:  # noqa: BLE001
        return []
    open_symbols = set(s.execute(
        select(Trade.symbol).where(Trade.status.in_([TradeStatus.filled, TradeStatus.submitted]))
    ).scalars())
    closed: list[str] = []
    for p in positions:
        if p.symbol in open_symbols:
            continue
        try:
            client.close_position(p.symbol)
            closed.append(p.symbol)
        except Exception:  # noqa: BLE001
            continue
    return closed
