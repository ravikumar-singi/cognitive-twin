"""Fixtures. Every test gets a fresh database.

Set TEST_DATABASE_URL (a PostgreSQL server URL, database name ignored) to run the
suite against PostgreSQL through the real Alembic migrations; otherwise SQLite.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from cognitive_twin.app import create_app
from cognitive_twin.config import Settings

PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def pg_template():
    if not PG:
        yield None
        return
    from alembic import command
    from alembic.config import Config
    admin = create_engine(make_url(PG).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = "ct_tmpl_" + uuid.uuid4().hex[:8]
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(PG).set(database=name).render_as_string(hide_password=False)
    cfg = Config()
    cfg.set_main_option("script_location", "src/cognitive_twin/db/migrations")
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")  # the real migration, not create_all
    from cognitive_twin.db.session import engine_for
    engine_for(url).dispose()
    yield name
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def db_url(tmp_path, pg_template):
    if pg_template is None:
        yield f"sqlite:///{tmp_path}/ct.db"
        return
    admin = create_engine(make_url(PG).set(database="postgres"), isolation_level="AUTOCOMMIT")
    name = "ct_test_" + uuid.uuid4().hex[:8]
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{pg_template}"'))
    yield make_url(PG).set(database=name).render_as_string(hide_password=False)
    from cognitive_twin.db.session import engine_for
    engine_for.cache_clear()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def settings(db_url, tmp_path):
    return replace(Settings(), env="test", database_url=db_url,
                   local_key_dir=str(tmp_path / "keys"), dev_login=True, demo_enabled=True,
                   policy_cache_seconds=0)


@pytest.fixture
def make_client(settings):
    clients = []

    def _make(**overrides) -> TestClient:
        c = TestClient(create_app(replace(settings, **overrides)))
        c.__enter__()
        clients.append(c)
        return c
    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def login(c: TestClient, persona: str) -> dict:
    c.cookies.clear()
    r = c.post("/auth/dev-login", json={"persona": persona})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.headers["x-csrf-token"]}


def consent(c: TestClient, h: dict) -> None:
    r = c.post("/api/me/consent", json={"learning": True}, headers=h)
    assert r.status_code == 200, r.text


def answer_correctly(c: TestClient, h: dict, track, n: int) -> None:
    """Drive the teaching loop, answering every question correctly."""
    for _ in range(n):
        d = c.post("/api/me/next", headers=h).json()
        if d["action"] == "break":
            if d["proposed_action"] == "done":
                return
            c.post("/api/me/break", headers=h)
            continue
        item = track.items[d["item"]["id"]]
        r = c.post("/api/me/answer", json={"item_id": item.id, "choice": item.answer},
                   headers=h)
        assert r.status_code == 200, r.text
