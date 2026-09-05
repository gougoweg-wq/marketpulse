"""Точка входа: python -m marketpulse.cli <команда>"""
from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def cmd_init() -> None:
    from marketpulse.db.session import init_db
    from marketpulse.ingest.feeds import seed_sources

    init_db()
    n = seed_sources()
    print(f"База инициализирована, источников в каталоге: {n}")


def cmd_collect() -> None:
    from marketpulse.ingest.collector import collect_once

    summary = asyncio.run(collect_once())
    print(
        f"Источников: {summary['total_sources']}, ок: {summary['ok']}, "
        f"с ошибками: {summary['failed']}, новых материалов: {summary['new_articles']}"
    )


def cmd_nlp() -> None:
    from marketpulse.nlp.dedup import cluster_new_articles

    r = cluster_new_articles()
    print(
        f"Обработано статей: {r['processed']}, новых событий: {r['new_clusters']}, "
        f"перепечаток схлопнуто: {r['attached']}"
    )



def cmd_prices() -> None:
    from marketpulse.market.prices import fetch_prices

    r = fetch_prices()
    print(f"Баров добавлено: {r['inserted']}, ошибки: {r['failed']}")


def cmd_replay() -> None:
    from marketpulse.model.engine import replay_history

    r = replay_history()
    print(f"Разогрев: обучена на {r['trained']} событиях, пропущено {r['skipped']}")


def cmd_decide() -> None:
    from marketpulse.model.engine import make_decisions

    r = make_decisions()
    print(f"Новых решений: {r['created']}")


def cmd_outcomes() -> None:
    from marketpulse.model.engine import record_outcomes

    r = record_outcomes()
    print(f"Исходов зафиксировано: {r['recorded']}")



def cmd_trade() -> None:
    from marketpulse.trading.executor import close_expired_trades, execute_new_decisions

    r1 = execute_new_decisions()
    r2 = close_expired_trades()
    print(
        f"Открыто: {r1['opened']}, отклонено риском: {r1['skipped_risk']}, "
        f"закрыто: {r2['closed']}, PnL: ${r2['pnl']:+,.2f}"
    )


def cmd_run() -> None:
    """Основной цикл: сбор -> NLP -> цены -> решения -> сделки -> исходы."""
    import time

    import os

    from marketpulse.db.session import init_db
    from marketpulse.ingest.collector import collect_once
    from marketpulse.ingest.smartmoney import fetch_insiders, generate_copy_signals
    from marketpulse.market.prices import fetch_prices
    from marketpulse.model.engine import make_decisions, record_outcomes
    from marketpulse.nlp.dedup import cluster_new_articles
    from marketpulse.trading.executor import close_expired_trades, execute_new_decisions

    init_db()
    failures = 0  # подряд упавших тиков: после 5 выходим с ошибкой, чтобы эстафета не маскировала поломку
    interval_sec = 15 * 60
    # RUN_MAX_HOURS: в облаке задача живёт < 6 ч и передаёт эстафету следующей
    max_hours = float(os.environ.get("RUN_MAX_HOURS", "0") or 0)
    deadline = time.time() + max_hours * 3600 if max_hours else None
    print(f"Запуск основного цикла, тик каждые {interval_sec // 60} мин"
          + (f", лимит {max_hours} ч" if max_hours else ", Ctrl+C для остановки") + ".", flush=True)
    while True:
        if deadline and time.time() + interval_sec > deadline:
            print("[цикл] лимит времени — передаю эстафету", flush=True)
            break
        started = time.time()
        try:
            c = asyncio.run(collect_once())
            n = cluster_new_articles()
            p = fetch_prices()
            ins = asyncio.run(fetch_insiders())
            cp = generate_copy_signals()
            d = make_decisions()
            t1 = execute_new_decisions()
            o = record_outcomes()
            t2 = close_expired_trades()
            failures = 0
            print(
                f"[тик] новостей +{c['new_articles']}, событий +{n['new_clusters']}, "
                f"баров +{p['inserted']}, инсайдеров +{ins['added']}/{cp['created']}, "
                f"решений +{d['created']}, сделок +{t1['opened']}/-{t2['closed']}, "
                f"исходов +{o['recorded']}",
                flush=True,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — один сбой не должен убивать цикл
            failures += 1
            print(f"[тик] ошибка ({failures}/5): {type(exc).__name__}: {exc}", flush=True)
            try:
                # ошибка тика должна быть видна в базе и дашборде, а не только в stdout раннера
                from marketpulse.db.models import LogEntry
                from marketpulse.db.session import db_session
                with db_session() as s:
                    s.add(LogEntry(level="warn", component="loop",
                                   message=f"тик упал ({failures}/5): {type(exc).__name__}: {str(exc)[:300]}"))
            except Exception:  # noqa: BLE001
                pass
            if failures >= 5:
                print("[цикл] пять тиков подряд с ошибкой — останавливаюсь", flush=True)
                sys.exit(1)
        time.sleep(max(0, interval_sec - (time.time() - started)))



def cmd_tick() -> None:
    """Один полный проход конвейера и выход — режим облачного раннера."""
    from marketpulse.db.session import init_db
    from marketpulse.ingest.collector import collect_once
    from marketpulse.ingest.smartmoney import fetch_insiders, generate_copy_signals
    from marketpulse.market.prices import fetch_prices
    from marketpulse.model.engine import make_decisions, record_outcomes
    from marketpulse.nlp.dedup import cluster_new_articles
    from marketpulse.trading.executor import close_expired_trades, execute_new_decisions

    import time

    def timed(label, fn):
        t = time.time()
        r = fn()
        print(f"  {label}: {time.time() - t:.0f}s", flush=True)
        return r

    init_db()
    c = timed("сбор", lambda: asyncio.run(collect_once()))
    n = timed("дедупликация", cluster_new_articles)
    p = timed("котировки", fetch_prices)
    ins = timed("инсайдеры", lambda: asyncio.run(fetch_insiders()))
    cp = generate_copy_signals()
    d = timed("решения", make_decisions)
    t1 = timed("сделки", execute_new_decisions)
    o = timed("исходы", record_outcomes)
    t2 = close_expired_trades()
    print(
        f"[tick] новостей +{c['new_articles']}, событий +{n['new_clusters']}, "
        f"баров +{p['inserted']}, инсайдеров +{ins['added']}/{cp['created']}, "
        f"решений +{d['created']}, сделок +{t1['opened']}/-{t2['closed']}, "
        f"исходов +{o['recorded']}"
    )



def cmd_manual() -> None:
    """Ручная сделка на демо-счёте: manual SYMBOL long|short [часы=24] [сумма=5% капитала]."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from marketpulse.config import settings
    from marketpulse.db.models import Decision, DecisionReason, Direction, LogEntry, PriceBar, Trade, TradeStatus
    from marketpulse.db.session import db_session, init_db
    from marketpulse.trading.executor import _alpaca_client, _submit_alpaca, account_equity

    args = sys.argv[2:]
    if len(args) < 2 or args[1] not in ("long", "short"):
        print("Использование: manual SYMBOL long|short [часы] [сумма$]")
        sys.exit(1)
    symbol, direction = args[0].upper(), Direction(args[1])
    hours = int(args[2]) if len(args) > 2 else 24
    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_session() as s:
        bar = s.execute(select(PriceBar).where(PriceBar.symbol == symbol)
                        .order_by(PriceBar.ts.desc()).limit(1)).scalar()
        if bar is None:
            print(f"нет котировок по {symbol} — тикер не в вотчлисте?")
            sys.exit(1)
        notional = float(args[3]) if len(args) > 3 else account_equity(s) * settings.max_position_pct
        d = Decision(symbol=symbol, direction=direction, reason=DecisionReason.manual,
                     confidence=0.99, features={"by": "user"}, model_version="manual",
                     horizon_hours=hours, entry_price=bar.close, created_at=now)
        s.add(d)
        s.flush()
        t = Trade(decision_id=d.id, symbol=symbol, direction=direction, qty=notional / bar.close,
                  notional=notional, status=TradeStatus.filled, submitted_at=now, filled_at=now,
                  fill_price=bar.close)
        client = _alpaca_client()
        broker_note = "симулятор (ключей брокера нет)"
        if client is not None:
            order_id, err = _submit_alpaca(client, t)
            t.broker_order_id = order_id
            broker_note = f"Alpaca ордер {order_id}" if order_id else f"брокер отклонил: {err}"
        s.add(t)
        s.add(LogEntry(component="trading",
                       message=f"ручная сделка: {direction.value} {symbol} ${notional:,.0f} на {hours}ч — {broker_note}"))
    print(f"{direction.value.upper()} {symbol} ${notional:,.0f}, горизонт {hours}ч, вход по {bar.close:.2f} — {broker_note}")


COMMANDS = {
    "init": cmd_init,
    "collect": cmd_collect,
    "nlp": cmd_nlp,
    "prices": cmd_prices,
    "replay": cmd_replay,
    "decide": cmd_decide,
    "outcomes": cmd_outcomes,
    "trade": cmd_trade,
    "run": cmd_run,
    "tick": cmd_tick,
    "manual": cmd_manual,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Использование: python -m marketpulse.cli [{'|'.join(COMMANDS)}]")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
