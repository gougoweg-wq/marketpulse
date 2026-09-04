"""Подключение к базе и фабрика сессий."""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.config import settings
from marketpulse.db.models import Base

# pool_pre_ping: удалённый Postgres закрывает простаивающее соединение между тиками —
# без проверки первый запрос после сна падает с обрывом SSL
_is_pg = settings.database_url.startswith("postgresql")
_connect_args = {}
if _is_pg:
    # зависший сокет не должен держать тик до убийства задачи по таймауту
    _connect_args = {
        "connect_timeout": 10,
        "keepalives": 1, "keepalives_idle": 60, "keepalives_interval": 10, "keepalives_count": 3,
    }
engine = create_engine(
    settings.database_url, future=True, pool_pre_ping=True, pool_recycle=300,
    connect_args=_connect_args,
)

if _is_pg:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_conn, _record):
        # через пулер Neon startup-параметры запрещены — ставим лимит уже в сессии.
        # В autocommit, чтобы не оставить открытую транзакцию: иначе SQLAlchemy
        # не сможет переключить autocommit ("connection in transaction status ACTIVE")
        dbapi_conn.autocommit = True
        try:
            with dbapi_conn.cursor() as cur:
                cur.execute("SET statement_timeout = 120000")
        finally:
            dbapi_conn.autocommit = False
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def db_session() -> Session:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
