"""fpa: operate the data layer from a terminal.

  fpa migrate [--reset]                      create schemas, policies and the API role
  fpa seed [--if-empty]                      load the synthetic source systems and people
  fpa refresh [--period 2026-09] [--redraft] tie-out, coverage check, then publish
  fpa simulate drop-line --period 2026-09    lose one GL line between ERP and warehouse
  fpa simulate restore                       put dropped lines back
  fpa access review [--as PERSON] [--csv F]  who can see what, for the quarterly review
  fpa access grant PERSON TYPE [VALUE] --reason R --by PERSON [--expires DATE]
  fpa access revoke GRANT_ID --by PERSON
  fpa ask "QUESTION" --as PERSON             answer a question under that person's grants
  fpa package --period 2026-09 --as PERSON --out FILE.pptx
  fpa api                                    serve the HTTP API
"""

from __future__ import annotations

import argparse
import csv
import json
import sys

from fpa import ask as ask_module
from fpa import package, pipeline, reporting
from fpa.config import get_settings
from fpa.db import app_pool, owner_connect, scoped
from fpa.migrate import migrate
from fpa.seed import is_seeded, seed
from fpa.semantic import load_catalog


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_migrate(args) -> int:
    with owner_connect() as conn:
        _print({"applied": migrate(conn, reset=args.reset)})
    return 0


def cmd_seed(args) -> int:
    with owner_connect() as conn:
        if args.if_empty and is_seeded(conn):
            _print({"seeded": False, "reason": "source tables already have data"})
            return 0
        _print({"seeded": True, "rows": seed(conn)})
    return 0


def cmd_refresh(args) -> int:
    with owner_connect() as conn:
        period = reporting.parse_period(args.period) if args.period else None
        result = pipeline.refresh(conn, period, redraft=args.redraft)
    _print(result)
    return 0 if result["status"] == "published" else 2


def cmd_simulate(args) -> int:
    with owner_connect() as conn:
        if args.what == "drop-line":
            _print(pipeline.drop_line(conn, reporting.parse_period(args.period)))
        else:
            _print({"restored_lines": pipeline.restore_lines(conn)})
    return 0


def cmd_access(args) -> int:
    if args.action == "review":
        pool = app_pool(max_size=1)
        try:
            with scoped(pool, args.as_person) as conn:
                rows = conn.execute("select * from sec.access_review()").fetchall()
        finally:
            pool.close()
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            _print({"written": args.csv, "rows": len(rows)})
        else:
            _print(rows)
        return 0
    with owner_connect() as conn, conn.transaction():
        conn.execute("select set_config('app.actor', %s, true)", (args.by,))
        if args.action == "grant":
            row = conn.execute(
                "insert into sec.access_grant (person_id, scope_type, scope_value, reason, granted_by, expires_at)"
                " values (%s, %s, %s, %s, %s, %s) returning grant_id",
                (args.person, args.scope_type, args.scope_value, args.reason, args.by, args.expires),
            ).fetchone()
            _print({"granted": row["grant_id"]})
        else:
            n = conn.execute("delete from sec.access_grant where grant_id = %s", (args.grant_id,)).rowcount
            _print({"revoked": n})
    return 0


def cmd_ask(args) -> int:
    settings, catalog = get_settings(), load_catalog()
    pool = app_pool(max_size=1)
    try:
        with scoped(pool, args.as_person) as conn:
            me = reporting.whoami(conn)
            if me is None:
                print(f"{args.as_person} is not an active person", file=sys.stderr)
                return 1
            latest = reporting.periods(conn)[0]
            try:
                _print(ask_module.ask(conn, catalog, args.question, me, latest, settings.budget_version))
            except ask_module.NotUnderstood as exc:
                _print({"answer": str(exc)})
    finally:
        pool.close()
    return 0


def cmd_package(args) -> int:
    settings, catalog = get_settings(), load_catalog()
    pool = app_pool(max_size=1)
    try:
        with scoped(pool, args.as_person) as conn:
            me = reporting.whoami(conn)
            content = package.build(conn, catalog, reporting.parse_period(args.period), settings.budget_version, me)
    except package.PackageBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        pool.close()
    with open(args.out, "wb") as fh:
        fh.write(content)
    _print({"written": args.out, "bytes": len(content)})
    return 0


def cmd_api(args) -> int:
    from fpa.api import main as serve

    serve()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fpa", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate")
    p.add_argument("--reset", action="store_true", help="drop and recreate every schema")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("seed")
    p.add_argument("--if-empty", action="store_true")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("refresh")
    p.add_argument("--period")
    p.add_argument("--redraft", action="store_true", help="rewrite unreviewed drafts, e.g. after switching drafter")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("simulate")
    p.add_argument("what", choices=["drop-line", "restore"])
    p.add_argument("--period", default="2026-09")
    p.set_defaults(func=cmd_simulate)

    p = sub.add_parser("access")
    asub = p.add_subparsers(dest="action", required=True)
    r = asub.add_parser("review")
    r.add_argument("--as", dest="as_person", default="sam.keller@example.com")
    r.add_argument("--csv")
    g = asub.add_parser("grant")
    g.add_argument("person")
    g.add_argument("scope_type", choices=["all", "business_unit", "department", "cost_center"])
    g.add_argument("scope_value", nargs="?")
    g.add_argument("--reason", required=True)
    g.add_argument("--by", required=True)
    g.add_argument("--expires")
    v = asub.add_parser("revoke")
    v.add_argument("grant_id", type=int)
    v.add_argument("--by", required=True)
    p.set_defaults(func=cmd_access)

    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--as", dest="as_person", required=True)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("package")
    p.add_argument("--period", required=True)
    p.add_argument("--as", dest="as_person", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("api")
    p.set_defaults(func=cmd_api)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
