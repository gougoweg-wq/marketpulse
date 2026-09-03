"""Исполнение решений на демо-счёте.

Два режима:
  - внутренний симулятор (по умолчанию): виртуальные $100k, сделки
    закрываются по горизонту решения, PnL считается из realized_return;
  - Alpaca paper (если заданы ключи): ордера дублируются на реальный
    демо-счёт брокера. Реальная торговля в коде отсутствует.

Размер позиции: пропорционален уверенности модели, исследовательские
сделки — четверть размера. Лимиты: на позицию и на суммарную экспозицию.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from marketpulse.config import settings
from marketpulse.db.models import (
    Decision, DecisionReason, Direction, LogEntry, Trade, TradeStatus,
)
from marketpulse.db.session import db_session

STARTING_EQUITY = 100_000.0


def account_equity(s) -> float:
    """Текущий капитал симулятора = старт + сумма PnL закрытых сделок."""
    pnl = 0.0
    for t in s.execute(select(Trade).where(Trade.status == TradeStatus.closed)).scalars():
        pnl += t.pnl or 0.0
    return STARTING_EQUITY + pnl


def open_exposure(s) -> float:
    exp = 0.0
    for t in s.execute(select(Trade).where(Trade.status == TradeStatus.filled)).scalars():
        exp += t.notional
    return exp


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


def _submit_alpaca(client, trade: Trade) -> str | None:
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        side = OrderSide.BUY if trade.direction == Direction.long else OrderSide.SELL
        order = client.submit_order(MarketOrderRequest(
            symbol=trade.symbol, notional=round(trade.notional, 2),
            side=side, time_in_force=TimeInForce.DAY,
        ))
        return str(order.id)
    except Exception:
        return None


def execute_new_decisions() -> dict:
    """Открывает сделки по решениям long/short без сделки."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    opened = 0
    skipped_risk = 0
    alpaca = _alpaca_client()

    with db_session() as s:
        equity = account_equity(s)
        exposure = open_exposure(s)
        traded = {t.decision_id for t in s.execute(select(Trade)).scalars()}

        pending = s.execute(
            select(Decision).where(
                Decision.direction != Direction.flat,
                Decision.outcome_recorded_at.is_(None),
                Decision.entry_price.isnot(None),
            )
        ).scalars().all()

        for d in pending:
            if d.id in traded:
                continue
            # размер: доля уверенности сверх порога, эксплорейшн — четверть
            base = equity * settings.max_position_pct
            conf_frac = min(1.0, max(0.2, (d.confidence - 0.5) / 0.15))
            notional = base * conf_frac
            if d.reason == DecisionReason.exploration:
                notional *= 0.25
            if exposure + notional > equity * settings.max_gross_exposure:
                skipped_risk += 1
                continue

            qty = notional / d.entry_price
            trade = Trade(
                decision_id=d.id, symbol=d.symbol, direction=d.direction,
                qty=qty, notional=notional, status=TradeStatus.filled,
                filled_at=now, fill_price=d.entry_price,
            )
            if alpaca is not None:
                trade.broker_order_id = _submit_alpaca(alpaca, trade)
            s.add(trade)
            exposure += notional
            opened += 1

        if opened or skipped_risk:
            s.add(LogEntry(
                component="trading",
                message=f"открыто сделок: {opened}, отклонено риск-лимитом: {skipped_risk}, "
                        f"капитал=${equity:,.0f}, экспозиция=${exposure:,.0f}",
                payload={"opened": opened, "skipped": skipped_risk, "equity": equity},
            ))
    return {"opened": opened, "skipped_risk": skipped_risk}


def close_expired_trades() -> dict:
    """Закрывает сделки, чьи решения получили исход."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    closed = 0
    total_pnl = 0.0

    with db_session() as s:
        open_trades = s.execute(
            select(Trade).where(Trade.status == TradeStatus.filled)
        ).scalars().all()
        for t in open_trades:
            d = s.get(Decision, t.decision_id)
            if d is None or d.realized_return is None:
                continue
            t.status = TradeStatus.closed
            # время закрытия = конец горизонта решения, а не момент запуска скрипта
            t.closed_at = d.created_at.replace(tzinfo=None) + __import__("datetime").timedelta(hours=d.horizon_hours)
            t.close_price = d.exit_price
            t.pnl = t.notional * d.realized_return
            total_pnl += t.pnl
            closed += 1

        if closed:
            s.add(LogEntry(
                component="trading",
                message=f"закрыто сделок: {closed}, PnL за партию: ${total_pnl:+,.2f}",
                payload={"closed": closed, "pnl": total_pnl},
            ))
    return {"closed": closed, "pnl": total_pnl}
