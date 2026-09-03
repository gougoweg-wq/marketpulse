"""Асинхронный сборщик новостей.

Тянет все активные источники параллельно (с ограничением),
парсит RSS/Atom через feedparser, телеграм-каналы — через HTML
веб-превью t.me/s/. Ошибки не валят процесс: источник получает
+1 к error_streak и после 10 неудач подряд отключается.
"""
from __future__ import annotations

import asyncio
import logging
import re
from calendar import timegm
from datetime import datetime, timedelta, timezone

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from sqlalchemy import select

from marketpulse.config import settings
from marketpulse.db.models import Article, LogEntry, Source, SourceKind
from marketpulse.db.session import db_session

log = logging.getLogger("ingest")

MAX_ERROR_STREAK = 10


def _parse_rss(source: Source, raw: bytes) -> list[dict]:
    """RSS/Atom -> список статей."""
    parsed = feedparser.parse(raw)
    items = []
    for e in parsed.entries[:50]:
        ext_id = e.get("id") or e.get("link") or e.get("title", "")
        if not ext_id:
            continue
        published = None
        for key in ("published_parsed", "updated_parsed"):
            t = e.get(key)
            if t:
                published = datetime.fromtimestamp(timegm(t), tz=timezone.utc)
                break
        body = ""
        if e.get("summary"):
            body = BeautifulSoup(e["summary"], "lxml").get_text(" ", strip=True)
        items.append({
            "external_id": ext_id[:500],
            "title": e.get("title", "")[:2000],
            "body": body[:5000],
            "url": e.get("link", "")[:1000],
            "published_at": published,
        })
    return items


_TG_TIME_RE = re.compile(r"datetime=\"([^\"]+)\"")


def _parse_telegram(source: Source, raw: bytes) -> list[dict]:
    """Веб-превью t.me/s/<канал> -> список постов."""
    soup = BeautifulSoup(raw, "lxml")
    items = []
    for msg in soup.select(".tgme_widget_message")[-50:]:
        post_id = msg.get("data-post", "")
        if not post_id:
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if not text:
            continue
        published = None
        time_el = msg.select_one("time[datetime]")
        if time_el and time_el.get("datetime"):
            try:
                published = datetime.fromisoformat(time_el["datetime"])
            except ValueError:
                pass
        items.append({
            "external_id": post_id[:500],
            "title": text[:300],
            "body": text[:5000],
            "url": f"https://t.me/{post_id}",
            "published_at": published,
        })
    return items


async def _fetch_one(
    session: aiohttp.ClientSession, sem: asyncio.Semaphore, src_id: int,
    kind: SourceKind, url: str,
) -> tuple[int, list[dict] | None, str]:
    """Возвращает (source_id, items | None, статус)."""
    async with sem:
        for attempt in range(settings.fetch_retries + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(
                        total=settings.fetch_timeout_sec)) as resp:
                    if resp.status != 200:
                        status = f"http {resp.status}"
                        if attempt < settings.fetch_retries and resp.status >= 500:
                            await asyncio.sleep(1 + attempt)
                            continue
                        return src_id, None, status
                    raw = await resp.read()
            except asyncio.TimeoutError:
                if attempt < settings.fetch_retries:
                    continue
                return src_id, None, "timeout"
            except aiohttp.ClientError as exc:
                return src_id, None, f"error: {type(exc).__name__}"

            try:
                # feedparser/bs4 — синхронные и бывают медленными; уводим в поток
                if kind == SourceKind.telegram:
                    items = await asyncio.to_thread(_parse_telegram, None, raw)
                else:
                    items = await asyncio.to_thread(_parse_rss, None, raw)
                return src_id, items, "ok"
            except Exception as exc:  # noqa: BLE001 — плохая разметка не должна валить сбор
                return src_id, None, f"parse: {type(exc).__name__}"
    return src_id, None, "unreachable"


async def collect_once() -> dict:
    """Один проход по всем активным источникам. Возвращает сводку."""
    with db_session() as s:
        sources = s.execute(
            select(Source.id, Source.kind, Source.url).where(Source.enabled == 1)
        ).all()

    sem = asyncio.Semaphore(settings.fetch_concurrency)
    headers = {"User-Agent": settings.user_agent}
    async with aiohttp.ClientSession(headers=headers) as http:
        results = await asyncio.gather(*[
            _fetch_one(http, sem, sid, kind, url) for sid, kind, url in sources
        ])

    now = datetime.now(timezone.utc)
    new_articles = 0
    ok_sources = 0
    failed = 0

    with db_session() as s:
        for src_id, items, status in results:
            src = s.get(Source, src_id)
            src.last_fetched_at = now
            src.last_status = status
            if items is None:
                failed += 1
                src.error_streak += 1
                if src.error_streak >= MAX_ERROR_STREAK:
                    src.enabled = 0
                    s.add(LogEntry(
                        level="warn", component="ingest",
                        message=f"источник отключён после {MAX_ERROR_STREAK} ошибок: {src.name}",
                    ))
                continue

            ok_sources += 1
            src.error_streak = 0
            existing = {
                row for row in s.execute(
                    select(Article.external_id).where(
                        Article.source_id == src_id,
                        Article.fetched_at >= now - timedelta(days=14),
                    )
                ).scalars()
            }
            for item in items:
                if item["external_id"] in existing:
                    continue
                existing.add(item["external_id"])  # дубли и внутри одной ленты
                s.add(Article(source_id=src_id, fetched_at=now, **item))
                new_articles += 1

        s.add(LogEntry(
            component="ingest",
            message=f"сбор завершён: {ok_sources} источников ок, {failed} с ошибками, "
                    f"{new_articles} новых материалов",
            payload={"ok": ok_sources, "failed": failed, "new": new_articles},
        ))

    return {"ok": ok_sources, "failed": failed, "new_articles": new_articles,
            "total_sources": len(sources)}
