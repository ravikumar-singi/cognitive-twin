"""Least-privilege database role for the running service (requirement R1.2).

Migrations run as the database owner. The service connects as ``ct_app``,
which may only SELECT and INSERT on append-only tables: database permissions,
not just triggers, prevent updates and deletes on ledger rows.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from .models import APPEND_ONLY_TABLES, Base

APP_ROLE = "ct_app"


def _grant_plan() -> tuple[list[str], list[str]]:
    mutable = sorted(t for t in Base.metadata.tables if t not in APPEND_ONLY_TABLES)
    return mutable, list(APPEND_ONLY_TABLES)


def create_app_role(engine: Engine, password: str) -> None:
    """Create or update ``ct_app`` with least-privilege grants.

    Uses psycopg's SQL composition (identifiers and literals are quoted by the
    driver), because role DDL cannot take bind parameters.
    """
    if engine.dialect.name != "postgresql":
        raise RuntimeError("the application role is only used with PostgreSQL")
    if len(password) < 16:
        raise ValueError("CT_APP_DB_PASSWORD must be at least 16 characters")
    from psycopg import sql
    mutable, append_only = _grant_plan()
    role = sql.Identifier(APP_ROLE)
    tables = lambda names: sql.SQL(", ").join(sql.Identifier(n) for n in names)  # noqa: E731
    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        db = conn.execute(text("SELECT current_database()")).scalar_one()
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
                              {"r": APP_ROLE}).scalar()
        stmts = [] if exists else [sql.SQL("CREATE ROLE {} LOGIN").format(role)]
        stmts += [
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role, sql.Literal(password)),
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), role),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role),
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                tables(mutable), role),
            sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(tables(append_only), role),
            sql.SQL("GRANT SELECT ON alembic_version TO {}").format(role),
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(role),
        ]
        for stmt in stmts:
            conn.exec_driver_sql(stmt.as_string(raw))
