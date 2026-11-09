"""Engine, sessions and schema bootstrap."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import APPEND_ONLY_TABLES, Base


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn, _):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
        return engine
    # Size so that instances x workers x (pool + overflow) stays under the database's
    # max_connections; see docs/production.md.
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                         pool_size=int(os.environ.get("CT_DB_POOL_SIZE", "5")),
                         max_overflow=int(os.environ.get("CT_DB_MAX_OVERFLOW", "5")),
                         pool_timeout=10,
                         # Safety net: the server ends any transaction left idle for
                         # 30 s, so a leaked session can never pin a connection forever.
                         connect_args={"options": "-c idle_in_transaction_session_timeout=30000"
                                                  " -c statement_timeout=15000"})


@lru_cache(maxsize=8)
def engine_for(url: str) -> Engine:
    return make_engine(url)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# --- append-only enforcement --------------------------------------------------------------

PG_FUNCTION = """
CREATE OR REPLACE FUNCTION ct_reject_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only (%)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""


def append_only_ddl(dialect: str) -> list[str]:
    stmts: list[str] = []
    if dialect == "postgresql":
        stmts.append(PG_FUNCTION)
        for t in APPEND_ONLY_TABLES:
            stmts += [
                f"DROP TRIGGER IF EXISTS {t}_no_mutation ON {t}",
                f"CREATE TRIGGER {t}_no_mutation BEFORE UPDATE OR DELETE ON {t} "
                f"FOR EACH ROW EXECUTE FUNCTION ct_reject_mutation()",
                f"DROP TRIGGER IF EXISTS {t}_no_truncate ON {t}",
                f"CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION ct_reject_mutation()",
            ]
    elif dialect == "sqlite":
        for t in APPEND_ONLY_TABLES:
            for op in ("UPDATE", "DELETE"):
                stmts += [
                    f"DROP TRIGGER IF EXISTS {t}_no_{op.lower()}",
                    f"CREATE TRIGGER {t}_no_{op.lower()} BEFORE {op} ON {t} "
                    f"BEGIN SELECT RAISE(ABORT, 'table {t} is append-only'); END",
                ]
    return stmts


def install_append_only(engine: Engine) -> None:
    with engine.begin() as conn:
        for stmt in append_only_ddl(engine.dialect.name):
            conn.execute(text(stmt))


def create_all_for_tests(engine: Engine) -> None:
    """Fast schema setup for tests and local development. Production uses Alembic."""
    Base.metadata.create_all(engine)
    install_append_only(engine)
