"""Схема базы данных.

Центральная идея: полный журнал. Каждая новость, каждый признак,
каждое решение модели и его исход сохраняются — это и есть
обучающая выборка для онлайн-дообучения.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- источники


class SourceKind(str, enum.Enum):
    rss = "rss"
    telegram = "telegram"     # публичный канал через t.me/s/
    reddit = "reddit"
    hackernews = "hackernews"


class Source(Base):
    """Один источник новостей (RSS-лента, телеграм-канал и т.д.)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind), index=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500), unique=True)
    category: Mapped[str] = mapped_column(String(50), default="general")
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # доверие к источнику
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(200))
    error_streak: Mapped[int] = mapped_column(Integer, default=0)

    articles: Mapped[list[Article]] = relationship(back_populates="source")


# ---------------------------------------------------------------- новости


class Article(Base):
    """Сырая новость/пост до дедупликации."""

    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_article_source_ext"),
        Index("ix_articles_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(500))  # guid/link из ленты
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1000), default="")
    # published_at — время ПУБЛИКАЦИИ (из ленты), fetched_at — когда нашли мы.
    # Для обучения используем только fetched_at: модель не должна видеть
    # новость раньше, чем реально могла бы её получить.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    simhash: Mapped[int | None] = mapped_column(BigInteger, index=True)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("news_clusters.id"), index=True)

    source: Mapped[Source] = relationship(back_populates="articles")
    cluster: Mapped[NewsCluster | None] = relationship(back_populates="articles")


class NewsCluster(Base):
    """Кластер перепечаток одной новости. Единица события для модели."""

    __tablename__ = "news_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    representative_title: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    n_articles: Mapped[int] = mapped_column(Integer, default=1)
    n_sources: Mapped[int] = mapped_column(Integer, default=1)
    tickers: Mapped[list] = mapped_column(JSON, default=list)   # упомянутые тикеры
    sentiment: Mapped[float | None] = mapped_column(Float)      # [-1, 1]
    novelty: Mapped[float | None] = mapped_column(Float)        # насколько неожиданная

    articles: Mapped[list[Article]] = relationship(back_populates="cluster")


# ---------------------------------------------------------------- рынок


class PriceBar(Base):
    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_bar"),
        Index("ix_bars_symbol_ts", "symbol", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20))
    interval: Mapped[str] = mapped_column(String(10))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


# ---------------------------------------------------------------- решения и сделки


class Direction(str, enum.Enum):
    long = "long"
    short = "short"
    flat = "flat"


class DecisionReason(str, enum.Enum):
    model = "model"             # обычное решение модели
    exploration = "exploration" # исследовательская сделка (случайная)
    contrarian = "contrarian"   # контрарианский сигнал против толпы
    copy = "copy"               # копирование сделки инсайдера (Form 4)


class Decision(Base):
    """Каждое решение модели — вошло оно в сделку или нет.

    Снимок признаков хранится целиком: после исхода строка становится
    обучающим примером.
    """

    __tablename__ = "decisions"
    __table_args__ = (Index("ix_decisions_ts", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("news_clusters.id"))
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[Direction] = mapped_column(Enum(Direction))
    reason: Mapped[DecisionReason] = mapped_column(Enum(DecisionReason))
    confidence: Mapped[float] = mapped_column(Float)         # p(движение в нашу сторону)
    features: Mapped[dict] = mapped_column(JSON)             # снимок всех признаков
    model_version: Mapped[str] = mapped_column(String(50), default="v0")
    # исход (заполняется после горизонта предсказания)
    horizon_hours: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    realized_return: Mapped[float | None] = mapped_column(Float)  # с учётом направления
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    trade: Mapped[Trade | None] = relationship(back_populates="decision")


class TradeStatus(str, enum.Enum):
    submitted = "submitted"
    filled = "filled"
    closed = "closed"
    rejected = "rejected"


class Trade(Base):
    """Реально отправленный на демо-счёт ордер."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), unique=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[Direction] = mapped_column(Enum(Direction))
    qty: Mapped[float] = mapped_column(Float)
    notional: Mapped[float] = mapped_column(Float)
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus), default=TradeStatus.submitted)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fill_price: Mapped[float | None] = mapped_column(Float)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_price: Mapped[float | None] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float)

    decision: Mapped[Decision] = relationship(back_populates="trade")


# ---------------------------------------------------------------- журнал


class LogEntry(Base):
    """Лента событий для дашборда."""

    __tablename__ = "log_entries"
    __table_args__ = (Index("ix_logs_ts", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    level: Mapped[str] = mapped_column(String(10), default="info")
    component: Mapped[str] = mapped_column(String(50))   # ingest / model / trading ...
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)


class ModelMetric(Base):
    """Качество модели во времени — для графика деградации/дрейфа."""

    __tablename__ = "model_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model_version: Mapped[str] = mapped_column(String(50))
    window: Mapped[str] = mapped_column(String(20))      # напр. "7d"
    n_decisions: Mapped[int] = mapped_column(Integer)
    hit_rate: Mapped[float | None] = mapped_column(Float)
    avg_return: Mapped[float | None] = mapped_column(Float)
    sharpe: Mapped[float | None] = mapped_column(Float)
    max_drawdown: Mapped[float | None] = mapped_column(Float)


class InsiderFiling(Base):
    """Сделка инсайдера из SEC Form 4 (обязательное раскрытие, EDGAR)."""

    __tablename__ = "insider_filings"
    __table_args__ = (UniqueConstraint("accession", name="uq_insider_accession"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    accession: Mapped[str] = mapped_column(String(100))
    filed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    company: Mapped[str] = mapped_column(String(300))
    symbol: Mapped[str | None] = mapped_column(String(20), index=True)
    insider_name: Mapped[str | None] = mapped_column(String(200))
    transaction_code: Mapped[str | None] = mapped_column(String(5))  # P=покупка, S=продажа
    shares: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    value_usd: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str] = mapped_column(String(500), default="")
    copied: Mapped[int] = mapped_column(Integer, default=0)  # создан ли сигнал


class ModelBlob(Base):
    """Сериализованная модель. В облаке диск не переживает запуск — веса живут в БД."""

    __tablename__ = "model_blob"

    id: Mapped[int] = mapped_column(primary_key=True)  # всегда 1
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[str] = mapped_column(String(50), default="v0")
    data: Mapped[bytes] = mapped_column(LargeBinary)
