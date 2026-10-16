"""Connections. The pipeline uses the owner; everything a person triggers uses fpa_app."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from fpa.config import get_settings


def owner_connect(dsn: str | None = None) -> psycopg.Connection:
    # Autocommit, so each `with conn.transaction()` block is a real transaction rather than
    # a savepoint inside an implicit one that nobody commits.
    return psycopg.connect(dsn or get_settings().owner_dsn, row_factory=dict_row, autocommit=True)


def app_pool(dsn: str | None = None, max_size: int = 8) -> ConnectionPool:
    return ConnectionPool(
        dsn or get_settings().app_dsn,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=True,
    )


@contextmanager
def scoped(pool: ConnectionPool, person_id: str) -> Iterator[psycopg.Connection]:
    """A transaction in which every query runs as `person_id`.

    set_config(..., true) is transaction-local, so the identity disappears at commit
    and a pooled connection never carries one person's scope into the next request.
    """
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute("select set_config('app.person_id', %s, true)", (person_id,))
            yield conn
