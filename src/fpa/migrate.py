"""Applies sql/*.sql in order, once each, and creates the API role."""

from __future__ import annotations

import psycopg
from psycopg import sql

from fpa.config import get_settings

APP_ROLE = "fpa_app"
SCHEMAS = ("ops", "sec", "core", "src")


def ensure_app_role(conn: psycopg.Connection, password: str) -> None:
    exists = conn.execute("select 1 from pg_roles where rolname = %s", (APP_ROLE,)).fetchone()
    verb = "alter" if exists else "create"
    conn.execute(
        sql.SQL(
            "{} role {} login password {} nosuperuser nocreatedb nocreaterole nobypassrls"
        ).format(sql.SQL(verb), sql.Identifier(APP_ROLE), sql.Literal(password))
    )


def migrate(conn: psycopg.Connection, reset: bool = False) -> list[str]:
    settings = get_settings()
    applied_now: list[str] = []
    with conn.transaction():
        if reset:
            for schema in SCHEMAS:
                conn.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(schema)))
            conn.execute("drop table if exists public.fpa_schema_migrations")
        ensure_app_role(conn, settings.app_password)
        conn.execute(
            "create table if not exists public.fpa_schema_migrations ("
            " filename text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["filename"] for r in conn.execute("select filename from public.fpa_schema_migrations")}
        for path in sorted(settings.sql_dir.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into public.fpa_schema_migrations (filename) values (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now
