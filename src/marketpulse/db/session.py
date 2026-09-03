"""Подключение к базе и фабрика сессий."""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.config import settings
from marketpulse.db.models import Base

# pool_pre_ping: удалённый Postgres закрывает простаивающее соединение между тиками —
# без проверки первый запрос после сна падает с обрывом SSL
engine = create_engine(
    settings.database_url, future=True, pool_pre_ping=True, pool_recycle=300,
)
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
