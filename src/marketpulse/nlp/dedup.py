"""Дедупликация: схлопывание перепечаток в кластеры-события.

Одну новость публикуют десятки изданий. Без схлопывания модель видит
40 «независимых» событий вместо одного — и переоценивает сигнал.

Алгоритм: SimHash по словам заголовка+тела. Похожие хэши (расстояние
Хэмминга <= порога) в пределах 48-часового окна попадают в один кластер.
Количество источников в кластере — само по себе признак (охват новости).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from marketpulse.db.models import Article, LogEntry, NewsCluster
from marketpulse.db.session import db_session
from marketpulse.nlp.sentiment import score_sentiment
from marketpulse.nlp.tickers import extract_tickers

HAMMING_THRESHOLD = 8
CLUSTER_WINDOW = timedelta(hours=48)

_WORD_RE = re.compile(r"[a-zа-яё0-9]+", re.I)
_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is",
    "are", "as", "at", "by", "with", "from", "it", "its", "this", "that",
    "и", "в", "на", "с", "по", "для", "от", "не", "что", "как", "из",
}


def simhash64(text: str) -> int:
    """64-битный SimHash по словам."""
    v = [0] * 64
    for token in _WORD_RE.findall(text.lower()):
        if token in _STOP or len(token) < 2:
            continue
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            v[bit] += 1 if (h >> bit) & 1 else -1
    out = 0
    for bit in range(64):
        if v[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _signed(h: int) -> int:
    """SQLite хранит INTEGER со знаком — приводим uint64 к int64."""
    return h - (1 << 64) if h >= (1 << 63) else h


def cluster_new_articles() -> dict:
    """Обрабатывает статьи без кластера: считает simhash, ищет пару, создаёт/пополняет кластер.

    Все данные окна грузятся двумя запросами — против удалённого Postgres
    построчные обращения (N+1) превращают тик в десятки минут.
    """
    from marketpulse.model.features import clean_text

    now = datetime.now(timezone.utc)
    window_start = now - CLUSTER_WINDOW
    n_new_clusters = 0
    n_attached = 0

    with db_session() as s:
        pending = s.execute(
            select(Article).where(Article.cluster_id.is_(None)).order_by(Article.fetched_at)
        ).scalars().all()
        if not pending:
            return {"processed": 0, "new_clusters": 0, "attached": 0}

        recent = {
            c.id: c for c in s.execute(
                select(NewsCluster).where(NewsCluster.first_seen_at >= window_start)
            ).scalars()
        }
        rep_hash: dict[int, int] = {}
        cluster_sources: dict[int, set[int]] = {}
        if recent:
            for cid, simhash, src_id in s.execute(
                select(Article.cluster_id, Article.simhash, Article.source_id)
                .where(Article.cluster_id.in_(list(recent)))
                .order_by(Article.id)
            ):
                if simhash is not None and cid not in rep_hash:
                    rep_hash[cid] = simhash % (1 << 64)
                cluster_sources.setdefault(cid, set()).add(src_id)

        # кандидаты для склейки: (кластер, хэш-представитель, источники)
        candidates: list[tuple[NewsCluster, int, set[int]]] = [
            (recent[cid], h, cluster_sources.get(cid, set())) for cid, h in rep_hash.items()
        ]

        for art in pending:
            text = f"{art.title} {art.body[:1000]}"
            h = simhash64(text)
            art.simhash = _signed(h)

            best, best_dist = None, HAMMING_THRESHOLD + 1
            for cand in candidates:
                d = hamming(h, cand[1])
                if d < best_dist:
                    best, best_dist = cand, d

            if best is not None:
                cluster, _, srcs = best
                art.cluster = cluster          # id проставится при общем flush
                cluster.n_articles += 1
                srcs.add(art.source_id)
                cluster.n_sources = len(srcs)
                n_attached += 1
            else:
                cleaned = clean_text(text)
                cluster = NewsCluster(
                    representative_title=art.title[:500],
                    first_seen_at=art.fetched_at or now,
                    n_articles=1, n_sources=1,
                    tickers=extract_tickers(cleaned),
                    sentiment=score_sentiment(cleaned),
                )
                s.add(cluster)
                art.cluster = cluster
                candidates.append((cluster, h, {art.source_id}))
                n_new_clusters += 1

        s.add(LogEntry(
            component="nlp",
            message=f"кластеризация: {len(pending)} статей -> "
                    f"{n_new_clusters} новых событий, {n_attached} перепечаток",
        ))

    return {"processed": len(pending), "new_clusters": n_new_clusters, "attached": n_attached}
