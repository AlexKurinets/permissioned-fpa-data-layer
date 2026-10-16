"""Read models for the API, the package and the question answerer.

Every function here runs inside `db.scoped(...)`, so the rows it sees are already
limited to the caller's grants. Nothing here filters by person on its own; that is
the database's job, and doing it twice would hide a broken policy.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date

import psycopg

from fpa.semantic import Catalog, is_favorable

SUMMARY_METRICS = ("net_revenue", "cost_of_revenue", "gross_profit", "operating_expenses", "personnel_cost",
                   "adjusted_ebitda")


def parse_period(text: str) -> date:
    year, month = text.split("-")
    return date(int(year), int(month), 1)


def whoami(conn: psycopg.Connection) -> dict | None:
    return conn.execute("select * from sec.whoami()").fetchone()


def periods(conn: psycopg.Connection) -> list[date]:
    return [r["period"] for r in conn.execute(
        "select distinct period from ops.refresh_run where status = 'published' order by period desc")]


def status(conn: psycopg.Connection) -> dict:
    latest = conn.execute("select * from ops.refresh_run order by started_at desc limit 1").fetchone()
    published = conn.execute(
        "select * from ops.refresh_run where status = 'published' order by started_at desc limit 1").fetchone()
    breaks = []
    if latest and latest["status"] == "failed_tieout":
        breaks = conn.execute(
            "select entity_id, period, account_number, check_name, warehouse_amount::float,"
            " erp_amount::float, difference::float from ops.tieout_break where run_id = %s"
            " order by period, entity_id, account_number", (latest["run_id"],)).fetchall()
    return {"latest_run": latest, "latest_published_run": published, "tieout_breaks": breaks}


def _variance(actual: float, budget: float, favorable_when: str | None) -> dict:
    variance = actual - budget
    return {"actual": round(actual, 2), "budget": round(budget, 2), "variance": round(variance, 2),
            "variance_pct": round(variance / abs(budget) * 100, 1) if budget else None,
            "favorable": is_favorable(favorable_when, variance)}


def _by_account(conn, table: str, period: date, version: str | None) -> dict[str, dict[str, float]]:
    sql = (f"select account_number,"
           f" coalesce(sum(amount) filter (where period = %(p)s), 0)::float as month,"
           f" coalesce(sum(amount), 0)::float as ytd"
           f" from {table} where period between %(ys)s and %(p)s")
    params = {"p": period, "ys": date(period.year, 1, 1)}
    if version:
        sql += " and version = %(v)s"
        params["v"] = version
    sql += " group by 1"
    return {r["account_number"]: r for r in conn.execute(sql, params)}


def bva_summary(conn: psycopg.Connection, catalog: Catalog, period: date, version: str) -> list[dict]:
    actual = _by_account(conn, "core.fact_actuals", period, None)
    budget = _by_account(conn, "core.fact_budget", period, version)
    out = []
    for key in SUMMARY_METRICS:
        metric = catalog.metrics[key]
        weights = catalog.metric_weights(key)
        row = {"key": key, "label": metric.label, "favorable_when": metric.favorable_when}
        for span in ("month", "ytd"):
            a = sum(w * actual.get(n, {}).get(span, 0.0) for n, w in weights.items())
            b = sum(w * budget.get(n, {}).get(span, 0.0) for n, w in weights.items())
            row[span] = _variance(a, b, metric.favorable_when)
        row["in_scope"] = any(abs(row[s][k]) > 0.005 for s in ("month", "ytd") for k in ("actual", "budget"))
        out.append(row)
    # A margin is meaningless for someone who sees costs but no revenue: it is just their costs, negated.
    if not out[0]["in_scope"]:
        for row in out:
            if row["key"] in ("gross_profit", "adjusted_ebitda"):
                row["in_scope"] = False
    return out


def bva_lines(conn: psycopg.Connection, catalog: Catalog, period: date, version: str, units: list[str]) -> list[dict]:
    """Budget vs actual by account, for each department the caller can see in full.

    Cost centers in departments the caller can only partly see are reported on their own,
    never rolled up into a department total.
    """
    rows = conn.execute(
        """
        with a as (
            select cost_center_id, account_number,
                   sum(amount) filter (where period = %(p)s) as month, sum(amount) as ytd
            from core.fact_actuals where period between %(ys)s and %(p)s and cost_center_id is not null
            group by 1, 2),
        b as (
            select cost_center_id, account_number,
                   sum(amount) filter (where period = %(p)s) as month, sum(amount) as ytd
            from core.fact_budget where period between %(ys)s and %(p)s and version = %(v)s
            group by 1, 2)
        select cc.cost_center_id, cc.name as cost_center_name, cc.department_id, cc.department_name,
               coalesce(a.account_number, b.account_number) as account_number,
               coalesce(a.month, 0)::float as actual, coalesce(b.month, 0)::float as budget,
               coalesce(a.ytd, 0)::float as ytd_actual, coalesce(b.ytd, 0)::float as ytd_budget
        from a full join b on a.cost_center_id = b.cost_center_id and a.account_number = b.account_number
        join core.dim_cost_center cc on cc.cost_center_id = coalesce(a.cost_center_id, b.cost_center_id)
        """,
        {"p": period, "ys": date(period.year, 1, 1), "v": version},
    ).fetchall()
    visible_departments = {u.split(":", 1)[1] for u in units if u.startswith("department:")}
    grouped: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        account = catalog.accounts[r["account_number"]]
        if account.statement != "income_statement" or "eliminate_on_consolidation" in account.tags:
            continue
        if r["department_id"] in visible_departments:
            key = ("department", r["department_id"], r["department_name"], r["account_number"])
        else:
            key = ("cost_center", r["cost_center_id"], r["cost_center_name"], r["account_number"])
        for field in ("actual", "budget", "ytd_actual", "ytd_budget"):
            grouped[key][field] += r[field]
    out = []
    for (unit_type, unit_id, unit_name, number), v in grouped.items():
        account = catalog.accounts[number]
        month = _variance(v["actual"], v["budget"], account.favorable_when)
        material = abs(month["variance"]) >= catalog.min_abs_variance and (
            month["variance_pct"] is None or abs(month["variance_pct"]) >= catalog.min_pct_variance * 100)
        out.append({
            "unit_type": unit_type, "unit_id": unit_id, "unit_name": unit_name,
            "account_number": number, "account_name": account.name, "section": account.section.name,
            "group": account.group, **month, "material": material,
            "ytd": _variance(v["ytd_actual"], v["ytd_budget"], account.favorable_when),
        })
    section_order = list(catalog.sections)
    out.sort(key=lambda r: (r["unit_type"], r["unit_name"], section_order.index(r["section"]), r["account_number"]))
    return out


def variance_list(conn: psycopg.Connection, period: date) -> list[dict]:
    return conn.execute(
        """
        select v.variance_id, v.period, v.department_id, d.department_name, v.account_number, a.name as account_name,
               v.actual::float, v.budget::float, v.variance::float, v.variance_pct::float, v.favorable, v.driver,
               v.draft, v.draft_source, v.validator_note, v.status, v.final_text, v.reviewed_by, v.reviewed_at,
               v.review_note, v.facts
        from core.variance_line v
        join (select distinct department_id, department_name from core.dim_cost_center) d using (department_id)
        join core.dim_account a using (account_number)
        where v.period = %s
        order by abs(v.variance) desc
        """,
        (period,),
    ).fetchall()


def review_variance(conn: psycopg.Connection, variance_id: int, action: str, person_id: str,
                    text: str | None, note: str | None) -> dict | None:
    status = {"accept": "accepted", "edit": "edited", "reject": "rejected"}[action]
    row = conn.execute(
        """
        update core.variance_line
        set status = %(status)s,
            final_text = case when %(status)s = 'accepted' then draft
                              when %(status)s = 'edited' then %(text)s end,
            reviewed_by = %(person)s, reviewed_at = now(), review_note = %(note)s
        where variance_id = %(id)s
        returning variance_id, status, final_text, reviewed_by, reviewed_at
        """,
        {"status": status, "text": text, "person": person_id, "note": note, "id": variance_id},
    ).fetchone()
    return row


def forecast_view(conn: psycopg.Connection, catalog: Catalog, period: date, version: str) -> dict:
    year_start, year_end = date(period.year, 1, 1), date(period.year, 12, 1)
    revenue_weights = catalog.metric_weights("net_revenue")
    personnel = catalog.metric_weights("personnel_cost")
    opex = {n: s for n, s in catalog.metric_weights("operating_expenses").items()
            if "non_cash" not in catalog.accounts[n].tags}
    monthly = conn.execute(
        """
        with w as (select * from unnest(%(nums)s::text[], %(signs)s::int[]) as w(account_number, sign))
        select 'actual' as kind, cc.business_unit_id as unit_id, cc.business_unit_name as unit_name, f.period,
               sum(f.amount * w.sign)::float as amount
        from core.fact_actuals f join core.dim_cost_center cc using (cost_center_id) join w using (account_number)
        where f.period between %(ys)s and %(p)s group by 1, 2, 3, 4
        union all
        select 'budget', cc.business_unit_id, cc.business_unit_name, f.period, sum(f.amount * w.sign)::float
        from core.fact_budget f join core.dim_cost_center cc using (cost_center_id) join w using (account_number)
        where f.version = %(v)s and f.period between %(ys)s and %(ye)s group by 1, 2, 3, 4
        order by 2, 4
        """,
        {"nums": list(revenue_weights), "signs": list(revenue_weights.values()), "ys": year_start,
         "ye": year_end, "p": period, "v": version},
    ).fetchall()
    names = {r["unit_id"]: r["unit_name"] for r in monthly}
    fc = conn.execute(
        "select unit_type, unit_id, line, period, amount::float, method from core.forecast order by unit_id, period"
    ).fetchall()
    backtest = conn.execute(
        "select unit_type, unit_id, line, method, mape::float as wape, origins, horizon from core.forecast_backtest"
        " order by unit_type, unit_id, method").fetchall()

    revenue = []
    for unit_id, unit_name in sorted(names.items()):
        actual_ytd = sum(r["amount"] for r in monthly if r["unit_id"] == unit_id and r["kind"] == "actual")
        budget_fy = sum(r["amount"] for r in monthly if r["unit_id"] == unit_id and r["kind"] == "budget")
        budget_rest = sum(r["amount"] for r in monthly if r["unit_id"] == unit_id and r["kind"] == "budget"
                          and r["period"] > period)
        rest = [r for r in fc if r["unit_type"] == "business_unit" and r["unit_id"] == unit_id
                and r["line"] == "net_revenue"]
        if not rest:
            continue
        forecast_rest = sum(r["amount"] for r in rest)
        scores = {b["method"]: b["wape"] for b in backtest
                  if b["unit_type"] == "business_unit" and b["unit_id"] == unit_id}
        revenue.append({
            "unit_id": unit_id, "unit_name": unit_name, "ytd_actual": round(actual_ytd, 2),
            "forecast_rest_of_year": round(forecast_rest, 2), "outlook": round(actual_ytd + forecast_rest, 2),
            "old_method_outlook": round(actual_ytd + budget_rest, 2),
            "fy_budget": round(budget_fy, 2), "outlook_vs_budget": round(actual_ytd + forecast_rest - budget_fy, 2),
            "wape": scores,
        })
    series = [{"unit_id": r["unit_id"], "unit_name": r["unit_name"], "period": r["period"], "kind": r["kind"],
               "amount": round(r["amount"], 2)} for r in monthly]
    series += [{"unit_id": r["unit_id"], "unit_name": names.get(r["unit_id"], r["unit_id"]), "period": r["period"],
                "kind": "forecast", "amount": round(r["amount"], 2)}
               for r in fc if r["unit_type"] == "business_unit" and r["line"] == "net_revenue"]

    dept_rows = conn.execute(
        """
        with w as (select * from unnest(%(nums)s::text[]) as w(account_number))
        select cc.department_id, cc.department_name,
               coalesce(sum(f.amount) filter (where f.account_number = any(%(pers)s)), 0)::float as personnel_ytd,
               coalesce(sum(f.amount) filter (where not f.account_number = any(%(pers)s)), 0)::float as other_ytd
        from core.fact_actuals f join core.dim_cost_center cc using (cost_center_id) join w using (account_number)
        where f.period between %(ys)s and %(p)s
        group by 1, 2
        """,
        {"nums": list(opex), "pers": list(personnel), "ys": year_start, "p": period},
    ).fetchall()
    dept_budget = {r["department_id"]: r["amount"] for r in conn.execute(
        """
        select cc.department_id, sum(f.amount)::float as amount
        from core.fact_budget f join core.dim_cost_center cc using (cost_center_id)
        where f.version = %s and f.period between %s and %s and f.account_number = any(%s)
        group by 1
        """,
        (version, year_start, year_end, list(opex)))}
    opex_rows = []
    for d in dept_rows:
        rest = [r for r in fc if r["unit_type"] == "department" and r["unit_id"] == d["department_id"]]
        if not rest:
            continue
        rest_total = sum(r["amount"] for r in rest)
        ytd = d["personnel_ytd"] + d["other_ytd"]
        opex_rows.append({
            "department_id": d["department_id"], "department_name": d["department_name"],
            "ytd_actual": round(ytd, 2), "forecast_rest_of_year": round(rest_total, 2),
            "outlook": round(ytd + rest_total, 2), "fy_budget": round(dept_budget.get(d["department_id"], 0.0), 2),
            "outlook_vs_budget": round(ytd + rest_total - dept_budget.get(d["department_id"], 0.0), 2),
        })
    opex_rows.sort(key=lambda r: r["department_name"])
    return {"period": period.isoformat(), "revenue": revenue, "revenue_series": series, "opex": opex_rows,
            "backtest": backtest}
