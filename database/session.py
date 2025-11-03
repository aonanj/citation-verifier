import os
from contextlib import contextmanager
from typing import Any, Dict, Generator, Tuple

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.orm import declarative_base, sessionmaker


def _resolve_database_url(raw_url: str | None) -> Tuple[URL, Dict[str, Any]]:
    """Normalize the DATABASE_URL environment variable for SQLAlchemy.

    Neon requires SSL and the psycopg driver. We upgrade plain postgres URLs
    to use the psycopg driver and inject sslmode=require when it is absent.
    """
    if not raw_url:
        default_url = "sqlite:///./citation_verifier.db"
        return make_url(default_url), {"check_same_thread": False}

    url = make_url(raw_url)

    if url.drivername.startswith("sqlite"):
        return url, {"check_same_thread": False}

    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")

    query = dict(url.query)
    if "sslmode" not in query and url.drivername.startswith("postgresql"):
        query["sslmode"] = "require"
        url = url.set(query=query)

    return url, {}


def _create_engine() -> Engine:
    url, connect_args = _resolve_database_url(os.getenv("DATABASE_URL"))
    return create_engine(url, future=True, pool_pre_ping=True, connect_args=connect_args)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
