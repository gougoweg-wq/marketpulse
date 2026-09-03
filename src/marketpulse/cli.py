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

    from marketpulse.ingest.collector import collect_once
    from marketpulse.market.prices import fetch_prices
    from marketpulse.model.engine import make_decisions, record_outcomes
    from marketpulse.nlp.dedup import cluster_new_articles
    from marketpulse.trading.executor import close_expired_trades, execute_new_decisions

    interval_sec = 15 * 60
    print(f"Запуск основного цикла, тик каждые {interval_sec // 60} мин. Ctrl+C для остановки.")
    while True:
        started = time.time()
        try:
            c = asyncio.run(collect_once())
            n = cluster_new_articles()
            p = fetch_prices()
            d = make_decisions()
            t1 = execute_new_decisions()
            o = record_outcomes()
            t2 = close_expired_trades()
            print(
                f"[тик] новостей +{c['new_articles']}, событий +{n['new_clusters']}, "
                f"баров +{p['inserted']}, решений +{d['created']}, "
                f"сделок +{t1['opened']}/-{t2['closed']}, исходов +{o['recorded']}"
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — цикл не должен умирать
            print(f"[тик] ошибка: {type(exc).__name__}: {exc}")
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
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(f"Использование: python -m marketpulse.cli [{'|'.join(COMMANDS)}]")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
