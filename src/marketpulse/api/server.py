"""API дашборда: данные + управление пайплайном кнопками."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from marketpulse.config import settings
from marketpulse.db.models import (
    Article, Decision, DecisionReason, Direction, InsiderFiling, LogEntry, ModelBlob,
    NewsCluster, PriceBar, Source, Trade, TradeStatus,
)
from marketpulse.db.session import db_session
from marketpulse.model.features import clean_text
from marketpulse.trading.executor import STARTING_EQUITY, account_equity, open_exposure

app = FastAPI(title="MarketPulse")
DASHBOARD = Path(__file__).resolve().parents[3] / "dashboard" / "index.html"

_CYR = re.compile("[а-яё]", re.I)


def _lang(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    return "ru" if sum(1 for c in text if _CYR.match(c)) / len(letters) > 0.3 else "en"


# ------------------------------------------------------------ задачи (кнопки)

JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _run_action(name: str):
    from marketpulse.ingest.collector import collect_once
    from marketpulse.ingest.smartmoney import fetch_insiders, generate_copy_signals
    from marketpulse.market.prices import fetch_prices
    from marketpulse.model.engine import make_decisions, record_outcomes
    from marketpulse.nlp.dedup import cluster_new_articles
    from marketpulse.trading.executor import close_expired_trades, execute_new_decisions

    actions = {
        "collect": lambda: asyncio.run(collect_once()),
        "nlp": cluster_new_articles,
        "prices": fetch_prices,
        "decide": make_decisions,
        "trade": lambda: {**execute_new_decisions(), **close_expired_trades()},
        "outcomes": record_outcomes,
        "insiders": lambda: {**asyncio.run(fetch_insiders()), **generate_copy_signals()},
    }
    fn = actions[name]
    try:
        result = fn()
        with _JOBS_LOCK:
            JOBS[name] = {**JOBS[name], "status": "done", "result": result,
                          "finished_at": time.time()}
    except Exception as exc:  # noqa: BLE001
        with _JOBS_LOCK:
            JOBS[name] = {**JOBS[name], "status": "error",
                          "result": {"error": f"{type(exc).__name__}: {exc}"},
                          "finished_at": time.time()}


ACTION_NAMES = {"collect", "nlp", "prices", "decide", "trade", "outcomes", "insiders"}


@app.post("/api/action/{name}")
def run_action(name: str):
    if name not in ACTION_NAMES:
        raise HTTPException(404, "нет такого действия")
    with _JOBS_LOCK:
        if JOBS.get(name, {}).get("status") == "running":
            return {"status": "already_running"}
        JOBS[name] = {"status": "running", "started_at": time.time(),
                      "result": None, "finished_at": None}
    threading.Thread(target=_run_action, args=(name,), daemon=True).start()
    return {"status": "started"}


@app.get("/api/jobs")
def jobs():
    with _JOBS_LOCK:
        return {**JOBS, "loop": LOOP_STATE}


# ------------------------------------------------------------ основной цикл

LOOP_STATE = {"running": False, "last_tick": None, "tick_no": 0, "interval_min": 15}
_LOOP_STOP = threading.Event()


def _loop_body():
    while not _LOOP_STOP.is_set():
        started = time.time()
        for step in ["collect", "nlp", "prices", "insiders", "decide", "trade", "outcomes"]:
            if _LOOP_STOP.is_set():
                break
            _run_action_sync(step)
        LOOP_STATE["last_tick"] = datetime.now(timezone.utc).isoformat()
        LOOP_STATE["tick_no"] += 1
        _LOOP_STOP.wait(max(0, LOOP_STATE["interval_min"] * 60 - (time.time() - started)))
    LOOP_STATE["running"] = False


def _run_action_sync(name: str):
    with _JOBS_LOCK:
        JOBS[name] = {"status": "running", "started_at": time.time(),
                      "result": None, "finished_at": None}
    _run_action(name)


@app.post("/api/loop/start")
def loop_start():
    if LOOP_STATE["running"]:
        return {"status": "already_running"}
    _LOOP_STOP.clear()
    LOOP_STATE["running"] = True
    threading.Thread(target=_loop_body, daemon=True).start()
    return {"status": "started"}


@app.post("/api/loop/stop")
def loop_stop():
    _LOOP_STOP.set()
    LOOP_STATE["running"] = False
    return {"status": "stopped"}


# ------------------------------------------------------------ данные


@app.get("/")
def index():
    return FileResponse(DASHBOARD)


@app.get("/api/summary")
def summary():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_session() as s:
        equity = account_equity(s)
        exposure = open_exposure(s)
        n_sources = s.execute(select(func.count(Source.id)).where(Source.enabled == 1)).scalar()
        n_articles = s.execute(select(func.count(Article.id))).scalar()
        n_events = s.execute(select(func.count(NewsCluster.id))).scalar()
        n_open = s.execute(select(func.count(Trade.id)).where(Trade.status == TradeStatus.filled)).scalar()
        n_closed = s.execute(select(func.count(Trade.id)).where(Trade.status == TradeStatus.closed)).scalar()
        articles_24h = s.execute(
            select(func.count(Article.id)).where(Article.fetched_at >= now - timedelta(hours=24))
        ).scalar()
        dirs = dict(s.execute(
            select(Decision.direction, func.count()).group_by(Decision.direction)
        ).all())
        wins = s.execute(select(func.count(Decision.id)).where(Decision.realized_return > 0)).scalar()
        done = s.execute(select(func.count(Decision.id)).where(Decision.realized_return.isnot(None))).scalar()
        n_insiders = s.execute(select(func.count(InsiderFiling.id))).scalar()
        blob = s.get(ModelBlob, 1)
        model_version = blob.version if blob else "v0"
    return {
        "equity": equity, "starting_equity": STARTING_EQUITY,
        "pnl": equity - STARTING_EQUITY, "exposure": exposure,
        "sources": n_sources, "articles": n_articles, "articles_24h": articles_24h,
        "events": n_events, "open_trades": n_open, "closed_trades": n_closed,
        "longs": dirs.get(Direction.long, 0), "shorts": dirs.get(Direction.short, 0),
        "flats": dirs.get(Direction.flat, 0),
        "hit_rate": (wins / done) if done else None,
        "insiders": n_insiders,
        "model_version": model_version,
    }


@app.get("/api/watchlist")
def watchlist():
    """Тикер-лента: последняя цена и изменение за 24ч."""
    out = []
    with db_session() as s:
        for sym in settings.watchlist:
            bars = s.execute(
                select(PriceBar).where(PriceBar.symbol == sym)
                .order_by(PriceBar.ts.desc()).limit(25)
            ).scalars().all()
            if not bars:
                continue
            last = bars[0].close
            prev = bars[-1].close if len(bars) >= 24 else bars[-1].close
            out.append({"symbol": sym, "price": last,
                        "change": (last / prev - 1) if prev else 0})
    return out


@app.get("/api/equity")
def equity_curve():
    points = [{"ts": None, "equity": STARTING_EQUITY}]
    eq = STARTING_EQUITY
    with db_session() as s:
        for t in s.execute(
            select(Trade).where(Trade.status == TradeStatus.closed).order_by(Trade.closed_at)
        ).scalars():
            eq += t.pnl or 0.0
            points.append({"ts": t.closed_at.isoformat() if t.closed_at else None, "equity": eq})
    return points


@app.get("/api/strategies")
def strategies():
    """Внутренний лидерборд: какая стратегия зарабатывает."""
    out = []
    with db_session() as s:
        for reason in DecisionReason:
            rows = s.execute(
                select(Decision.realized_return).where(
                    Decision.reason == reason, Decision.realized_return.isnot(None),
                    Decision.direction != Direction.flat,
                )
            ).scalars().all()
            n_open = s.execute(select(func.count(Decision.id)).where(
                Decision.reason == reason, Decision.outcome_recorded_at.is_(None),
                Decision.direction != Direction.flat,
            )).scalar()
            wins = sum(1 for r in rows if r > 0)
            out.append({
                "reason": reason.value, "closed": len(rows), "open": n_open,
                "hit_rate": wins / len(rows) if rows else None,
                "avg_return": sum(rows) / len(rows) if rows else None,
            })
    return out


@app.get("/api/decisions")
def decisions(limit: int = 60):
    from bisect import bisect_right

    with db_session() as s:
        rows = s.execute(select(Decision).order_by(Decision.id.desc()).limit(limit)).scalars().all()
        # сила сигнала: перцентиль |edge| среди последних 500 решений
        recent = s.execute(
            select(Decision.confidence).order_by(Decision.id.desc()).limit(500)
        ).scalars().all()
        edges = sorted(abs(c - 0.5) for c in recent)

        def strength(conf: float) -> int:
            if not edges:
                return 50
            rank = bisect_right(edges, abs(conf - 0.5)) / len(edges)
            return max(1, min(99, round(rank * 100)))

        out = []
        for d in rows:
            cluster = s.get(NewsCluster, d.cluster_id) if d.cluster_id else None
            title = None
            if cluster:
                title = clean_text(cluster.representative_title)[:130]
            elif d.reason == DecisionReason.copy:
                f = d.features or {}
                title = f"Инсайдер {f.get('insider', '?')} · код {f.get('code')} · ${f.get('value_usd', 0):,.0f}"
            out.append({
                "id": d.id, "symbol": d.symbol, "direction": d.direction.value,
                "reason": d.reason.value, "confidence": d.confidence,
                "strength": strength(d.confidence),
                "sentiment": (d.features or {}).get("sentiment"),
                "title": title,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "realized_return": d.realized_return,
            })
        return out


@app.get("/api/events")
def events(filter: str = "important", limit: int = 40):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with db_session() as s:
        q = select(NewsCluster).order_by(NewsCluster.id.desc())
        rows = s.execute(q.limit(900)).scalars().all()
        if filter == "important":
            # ранжирование: охват источников + тикеры + сила тональности,
            # только свежее окно — иначе топ навсегда займут старые события
            recent = [c for c in rows if c.first_seen_at
                      and (now - c.first_seen_at.replace(tzinfo=None)).total_seconds() < 48 * 3600]
            rows = sorted(
                recent or rows,
                key=lambda c: (
                    c.n_sources
                    + (2 if c.tickers else 0)
                    + (1 if abs(c.sentiment or 0) > 0.3 else 0)
                ),
                reverse=True,
            )
        out = []
        for c in rows:
            title = clean_text(c.representative_title).strip()
            if not title:
                continue
            lang = _lang(title)
            has_tickers = bool(c.tickers)
            if filter == "important" and not (has_tickers or c.n_sources >= 3):
                continue
            if filter == "tickers" and not has_tickers:
                continue
            if filter == "ru" and lang != "ru":
                continue
            if filter == "en" and lang != "en":
                continue
            out.append({
                "id": c.id, "title": title[:160], "lang": lang,
                "tickers": c.tickers, "sentiment": c.sentiment,
                "n_sources": c.n_sources, "n_articles": c.n_articles,
                "first_seen_at": c.first_seen_at.isoformat() if c.first_seen_at else None,
            })
            if len(out) >= limit:
                break
        return out


@app.get("/api/insiders")
def insiders(limit: int = 30):
    with db_session() as s:
        rows = s.execute(
            select(InsiderFiling).order_by(InsiderFiling.filed_at.desc()).limit(limit)
        ).scalars().all()
        return [{
            "symbol": f.symbol, "company": f.company, "insider": f.insider_name,
            "code": f.transaction_code, "shares": f.shares, "price": f.price,
            "value_usd": f.value_usd, "copied": bool(f.copied),
            "filed_at": f.filed_at.isoformat() if f.filed_at else None,
            "url": f.url,
        } for f in rows]


@app.get("/api/trades")
def trades(limit: int = 60):
    with db_session() as s:
        rows = s.execute(select(Trade).order_by(Trade.id.desc()).limit(limit)).scalars().all()
        out = []
        for t in rows:
            d = s.get(Decision, t.decision_id)
            out.append({
                "id": t.id, "symbol": t.symbol, "direction": t.direction.value,
                "notional": t.notional, "status": t.status.value,
                "fill_price": t.fill_price, "close_price": t.close_price,
                "pnl": t.pnl,
                "confidence": d.confidence if d else None,
                "reason": d.reason.value if d else None,
                "submitted_at": t.submitted_at.isoformat() if t.submitted_at else None,
            })
        return out


@app.get("/api/logs")
def logs(limit: int = 120):
    with db_session() as s:
        rows = s.execute(select(LogEntry).order_by(LogEntry.id.desc()).limit(limit)).scalars().all()
        return [{
            "ts": e.ts.isoformat() if e.ts else None, "level": e.level,
            "component": e.component, "message": e.message,
        } for e in rows]


@app.get("/api/sources")
def sources():
    with db_session() as s:
        rows = s.execute(select(Source)).scalars().all()
        return [{
            "name": r.name, "kind": r.kind.value, "category": r.category,
            "enabled": bool(r.enabled), "status": r.last_status,
            "error_streak": r.error_streak,
        } for r in rows]
