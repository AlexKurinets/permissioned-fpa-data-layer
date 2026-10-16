"""Tests run against a real Postgres: row-level security cannot be faked in memory.

The session fixture rebuilds the schema, seeds the synthetic company and publishes
September 2026 once. Tests that change state put it back before they finish.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date

import pytest

os.environ.setdefault("FPA_OWNER_DSN", "postgresql://fpa:fpa@localhost:5432/fpa")
os.environ.setdefault("FPA_APP_DSN", "postgresql://fpa_app:fpa_app@localhost:5432/fpa")
os.environ["FPA_LLM_PROVIDER"] = "template"
os.environ["FPA_AUTH_MODE"] = "dev"

from fpa import pipeline  # noqa: E402
from fpa.db import app_pool, owner_connect, scoped  # noqa: E402
from fpa.migrate import migrate  # noqa: E402
from fpa.seed import seed  # noqa: E402
from fpa.semantic import load_catalog  # noqa: E402

PERIOD = date(2026, 9, 1)
CFO = "maya.ortiz@example.com"
FPA_LEAD = "sam.keller@example.com"
ANALYST = "priya.nair@example.com"
VP_ENG = "dana.whitfield@example.com"
PLATFORM_MGR = "omar.haddad@example.com"
GM_NEWS = "leo.marsh@example.com"
AUDITOR = "hana.kowalski@example.com"
NO_GRANTS = "ben.foster@example.com"


@pytest.fixture(scope="session")
def published():
    with owner_connect() as conn:
        migrate(conn, reset=True)
        seed(conn)
        result = pipeline.refresh(conn, PERIOD)
    assert result["status"] == "published", result
    return result


@pytest.fixture
def owner(published):
    with owner_connect() as conn:
        yield conn


@pytest.fixture(scope="session")
def pool(published):
    p = app_pool(max_size=2)
    yield p
    p.close()


@pytest.fixture
def as_person(pool):
    @contextmanager
    def _as(person_id: str):
        with scoped(pool, person_id) as conn:
            yield conn
    return _as


@pytest.fixture(scope="session")
def catalog():
    return load_catalog()


@pytest.fixture(scope="session")
def api(published):
    from fastapi.testclient import TestClient

    from fpa.api import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def token(api):
    cache: dict[str, str] = {}

    def _token(person_id: str) -> dict[str, str]:
        if person_id not in cache:
            cache[person_id] = api.post("/auth/dev-token", json={"person_id": person_id}).json()["access_token"]
        return {"Authorization": f"Bearer {cache[person_id]}"}
    return _token
