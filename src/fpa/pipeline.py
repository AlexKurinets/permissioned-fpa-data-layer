"""The refresh: check, then publish everything in one transaction.

Two gates run before anything is written to the published layer:

  coverage  every account in the ERP chart, the GL and the budget has a definition
            in the semantic layer
  tie-out   the GL detail the adapters landed reproduces the ERP's own trial balance
            activity for every entity, period and account, and every entity's
            journals balance

If either fails, the run is recorded with the reason and the published layer keeps
the last good data. If both pass, dimensions, facts, headcount exceptions, variance
drafts and the forecast are replaced in one transaction, so a reader sees either the
old month or the new one, never half of each.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import psycopg
from psycopg.types.json import Jsonb

from fpa import forecast, headcount
from fpa.config import get_settings
from fpa.drafting import Drafter, draft_commentary, get_drafter
from fpa.semantic import Catalog, coverage_gaps, load_catalog
from fpa.variance import build_facts, department_lines, is_material


def latest_period(conn: psycopg.Connection) -> date:
    return conn.execute("select max(period) as p from src.erp_trial_balance").fetchone()["p"]


def check_coverage(conn: psycopg.Connection, catalog: Catalog) -> list[str]:
    numbers = [r["account_number"] for r in conn.execute(
        "select account_number from src.erp_accounts"
        " union select account_number from src.erp_gl_lines"
        " union select account_number from src.plan_budget")]
    return coverage_gaps(catalog, numbers)


def check_tieout(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        """
        with wh as (
            select entity_id, period, account_number, sum(amount) as amount
            from src.erp_gl_lines group by 1, 2, 3),
        erp as (
            select entity_id, period, account_number, net_activity as amount from src.erp_trial_balance)
        select coalesce(wh.entity_id, erp.entity_id) as entity_id,
               coalesce(wh.period, erp.period) as period,
               coalesce(wh.account_number, erp.account_number) as account_number,
               'activity_by_account' as check_name,
               coalesce(wh.amount, 0) as warehouse_amount,
               coalesce(erp.amount, 0) as erp_amount,
               coalesce(wh.amount, 0) - coalesce(erp.amount, 0) as difference
        from wh full join erp
          on wh.entity_id = erp.entity_id and wh.period = erp.period and wh.account_number = erp.account_number
        where coalesce(wh.amount, 0) <> coalesce(erp.amount, 0)
        union all
        select entity_id, period, null, 'journals_balance', sum(amount), 0, sum(amount)
        from src.erp_gl_lines group by 1, 2 having sum(amount) <> 0
        order by 2, 1, 3
        """
    ).fetchall()


def _finish(conn: psycopg.Connection, run_id: str, status: str, summary: dict) -> None:
    conn.execute(
        "update ops.refresh_run set status = %s, finished_at = now(), summary = %s where run_id = %s",
        (status, Jsonb(json.loads(json.dumps(summary, default=str))), run_id),
    )


def publish_dimensions(conn: psycopg.Connection, catalog: Catalog) -> None:
    conn.execute("delete from core.dim_entity")
    conn.execute("insert into core.dim_entity select entity_id, name, currency from src.erp_entities")
    conn.execute("delete from core.dim_cost_center")
    conn.execute(
        "insert into core.dim_cost_center select cost_center_id, name, department_id, department_name,"
        " business_unit_id, business_unit_name, entity_id from src.erp_cost_centers"
    )
    conn.execute("delete from core.dim_account")
    with conn.cursor() as cur:
        cur.executemany(
            "insert into core.dim_account (account_number, name, section, grp, statement, nature, reporting_sign,"
            " favorable_when, budgeted, tags, definition, nuances, synonyms, owner)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(a.number, a.name, a.section.name, a.group, a.statement, a.section.nature, a.reporting_sign,
              a.favorable_when, a.budgeted, sorted(a.tags), a.definition, list(a.nuances), list(a.synonyms),
              a.owner) for a in catalog.accounts.values()],
        )


def publish_facts(conn: psycopg.Connection, version: str) -> None:
    conn.execute("delete from core.fact_actuals")
    conn.execute(
        """
        insert into core.fact_actuals (period, entity_id, cost_center_id, account_number, amount)
        select g.period, g.entity_id, g.cost_center_id, g.account_number, sum(g.amount * a.reporting_sign)
        from src.erp_gl_lines g join core.dim_account a using (account_number)
        group by 1, 2, 3, 4
        """
    )
    conn.execute("delete from core.fact_gl_detail")
    conn.execute(
        """
        insert into core.fact_gl_detail
        select g.line_id, g.period, g.entity_id, g.cost_center_id, g.account_number, g.vendor, g.memo,
               g.amount * a.reporting_sign
        from src.erp_gl_lines g join core.dim_account a using (account_number)
        where a.statement = 'income_statement' and g.cost_center_id is not null
        """
    )
    conn.execute("delete from core.fact_budget")
    conn.execute(
        "insert into core.fact_budget select version, period, cost_center_id, account_number, amount"
        " from src.plan_budget where version = %s",
        (version,),
    )
    conn.execute("delete from core.fact_budget_headcount")
    conn.execute(
        "insert into core.fact_budget_headcount select version, period, cost_center_id, fte"
        " from src.plan_headcount where version = %s",
        (version,),
    )
    conn.execute("delete from core.employee")
    conn.execute(
        "insert into core.employee select employee_id, full_name, cost_center_id, title, level, start_date,"
        " term_date from src.hris_employees"
    )
    conn.execute("delete from core.employee_comp")
    conn.execute(
        "insert into core.employee_comp select employee_id, cost_center_id, annual_salary from src.hris_employees"
    )
    conn.execute("delete from core.requisition")
    conn.execute(
        "insert into core.requisition select req_id, cost_center_id, title, level, planned_start,"
        " budgeted_salary, status, filled_by from src.hris_requisitions"
    )


def publish_headcount_exceptions(conn: psycopg.Connection, period: date) -> int:
    rows = headcount.find_exceptions(conn, period)
    conn.execute("delete from core.headcount_exception where period = %s", (period,))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into core.headcount_exception (period, employee_id, cost_center_id, kind, detail)"
            " values (%s, %s, %s, %s, %s)",
            [(period, r["employee_id"], r["cost_center_id"], r["kind"], r["detail"]) for r in rows],
        )
    return len(rows)


def publish_variances(conn: psycopg.Connection, catalog: Catalog, period: date, version: str,
                      drafter: Drafter, redraft: bool = False) -> dict:
    existing = {(r["department_id"], r["account_number"]): r for r in conn.execute(
        "select department_id, account_number, actual::float, budget::float, status"
        " from core.variance_line where period = %s", (period,))}
    material = [line for line in department_lines(conn, catalog, period, version) if is_material(catalog, line)]
    keep = set()
    drafted = reused = rejected = 0
    for line in material:
        key = (line.department_id, line.account_number)
        keep.add(key)
        old = existing.get(key)
        unchanged = old and abs(old["actual"] - line.actual) < 0.005 and abs(old["budget"] - line.budget) < 0.005
        if unchanged and (old["status"] != "draft" or not redraft):
            reused += 1  # same numbers as the last run: keep the draft and any review
            continue
        facts = build_facts(conn, catalog, line, period, version)
        draft = draft_commentary(facts, drafter)
        drafted += 1
        rejected += draft.source == "template_after_rejection"
        conn.execute(
            """
            insert into core.variance_line (period, department_id, account_number, actual, budget, variance,
                variance_pct, favorable, driver, facts, draft, draft_source, validator_note)
            values (%(period)s, %(dept)s, %(acct)s, %(actual)s, %(budget)s, %(variance)s, %(pct)s, %(fav)s,
                    %(driver)s, %(facts)s, %(draft)s, %(source)s, %(note)s)
            on conflict (period, department_id, account_number) do update set
                actual = excluded.actual, budget = excluded.budget, variance = excluded.variance,
                variance_pct = excluded.variance_pct, favorable = excluded.favorable, driver = excluded.driver,
                facts = excluded.facts, draft = excluded.draft, draft_source = excluded.draft_source,
                validator_note = excluded.validator_note, status = 'draft', final_text = null,
                reviewed_by = null, reviewed_at = null,
                review_note = case when core.variance_line.status <> 'draft'
                                   then 'Numbers changed after review, so the line was reset to draft.' end
            """,
            {"period": period, "dept": line.department_id, "acct": line.account_number,
             "actual": round(line.actual, 2), "budget": round(line.budget, 2), "variance": round(line.variance, 2),
             "pct": round(line.variance_pct, 4) if line.variance_pct is not None else None,
             "fav": facts["favorable"], "driver": facts["driver"],
             "facts": Jsonb(json.loads(json.dumps(facts, default=str))), "draft": draft.text,
             "source": draft.source, "note": draft.note},
        )
    stale = [k for k, r in existing.items() if k not in keep and r["status"] == "draft"]
    for dept, acct in stale:
        conn.execute("delete from core.variance_line where period = %s and department_id = %s and account_number = %s",
                     (period, dept, acct))
    return {"material_lines": len(material), "drafted": drafted, "kept_from_last_run": reused,
            "drafts_rejected": rejected, "drafter": drafter.name}


def refresh(conn: psycopg.Connection, period: date | None = None, drafter: Drafter | None = None,
            redraft: bool = False) -> dict:
    """Run the gates and publish. `redraft` rewrites unreviewed drafts even when their numbers are unchanged."""
    settings = get_settings()
    catalog = load_catalog()
    period = period or latest_period(conn)
    run_id = f"run-{period:%Y%m}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
    with conn.transaction():
        conn.execute("insert into ops.refresh_run (run_id, period, status) values (%s, %s, 'running')",
                     (run_id, period))

    gaps = check_coverage(conn, catalog)
    if gaps:
        with conn.transaction():
            _finish(conn, run_id, "failed_coverage", {"unmapped_accounts": gaps})
        return {"run_id": run_id, "period": period.isoformat(), "status": "failed_coverage",
                "unmapped_accounts": gaps}

    breaks = check_tieout(conn)
    if breaks:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into ops.tieout_break (run_id, entity_id, period, account_number, check_name,"
                    " warehouse_amount, erp_amount, difference) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                    [(run_id, b["entity_id"], b["period"], b["account_number"], b["check_name"],
                      b["warehouse_amount"], b["erp_amount"], b["difference"]) for b in breaks],
                )
            _finish(conn, run_id, "failed_tieout", {"breaks": len(breaks)})
        return {"run_id": run_id, "period": period.isoformat(), "status": "failed_tieout", "breaks": len(breaks)}

    try:
        with conn.transaction():
            publish_dimensions(conn, catalog)
            publish_facts(conn, settings.budget_version)
            exceptions = publish_headcount_exceptions(conn, period)
            variances = publish_variances(conn, catalog, period, settings.budget_version,
                                          drafter or get_drafter(), redraft)
            forecasts = forecast.run(conn, catalog, period, settings.budget_version)
            summary = {"semantic_layer_version": catalog.version, "accounts_defined": len(catalog.accounts),
                       "headcount_exceptions": exceptions, "variances": variances, "forecast": forecasts}
            _finish(conn, run_id, "published", summary)
    except Exception as exc:
        with conn.transaction():
            _finish(conn, run_id, "failed", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    return {"run_id": run_id, "period": period.isoformat(), "status": "published", **summary}


def drop_line(conn: psycopg.Connection, period: date) -> dict:
    """Simulates a partial extract: one GL line goes missing between the ERP and the warehouse."""
    with conn.transaction():
        row = conn.execute(
            """
            select line_id, account_number, entity_id, amount from src.erp_gl_lines
            where period = %s and account_number = '6050' and vendor is not null
            order by abs(amount) desc limit 1
            """,
            (period,),
        ).fetchone()
        conn.execute("insert into src.dropped_lines select * from src.erp_gl_lines where line_id = %s",
                     (row["line_id"],))
        conn.execute("delete from src.erp_gl_lines where line_id = %s", (row["line_id"],))
    return {"dropped_line_id": row["line_id"], "account_number": row["account_number"],
            "entity_id": row["entity_id"], "amount": float(row["amount"])}


def restore_lines(conn: psycopg.Connection) -> int:
    with conn.transaction():
        n = conn.execute("insert into src.erp_gl_lines select * from src.dropped_lines").rowcount
        conn.execute("delete from src.dropped_lines")
    return n
