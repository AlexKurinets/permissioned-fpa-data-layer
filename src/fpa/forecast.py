"""Rolling forecast and its backtest.

Revenue is re-forecast line by line (business unit x revenue account) with plan
attainment: each remaining budget month is scaled by how that line has actually run
against budget over its last three budgeted months. The budget keeps its own
seasonality and event calendar, which finance already trusts, and the forecast
learns where the year has drifted from it.

The method it replaced assumed the remaining months would land on budget. The
backtest scores that, plan attainment, and a Holt-Winters challenger (additive damped
trend, additive 12-month seasonality) on the same months: from each of six origins,
forecast one to three months ahead and compare with what actually closed. The score
is WAPE, total absolute error over total actual revenue, pooled across business
units so that a miss in one unit is not cancelled by an opposite miss in another.

Operating expenses are forecast per department from the headcount plan: the last
three months' personnel cost per head, times the heads expected once open roles
fill, plus the non-personnel run-rate.
"""

from __future__ import annotations

import warnings
from datetime import date

import numpy as np
import pandas as pd
import psycopg
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from fpa.semantic import Catalog
from fpa.synthetic import add_months, month_end

ORIGINS = 6
HORIZON = 3
LOOKBACK = 3
METHODS = ("remaining_budget", "plan_attainment", "holt_winters")


def _frame(conn: psycopg.Connection, catalog: Catalog, table: str, version: str | None) -> pd.DataFrame:
    weights = catalog.metric_weights("net_revenue")
    sql = (f"select cc.business_unit_id as unit_id, f.account_number, f.period, sum(f.amount)::float as amount"
           f" from {table} f join core.dim_cost_center cc using (cost_center_id)"
           f" where f.account_number = any(%(nums)s)")
    params: dict = {"nums": list(weights)}
    if version:
        sql += " and f.version = %(v)s"
        params["v"] = version
    rows = conn.execute(sql + " group by 1, 2, 3", params).fetchall()
    frame = pd.DataFrame(rows, columns=["unit_id", "account_number", "period", "amount"])
    frame["amount"] *= frame["account_number"].map(weights)
    wide = frame.pivot_table(index="period", columns=["unit_id", "account_number"], values="amount", aggfunc="sum")
    wide.index = pd.DatetimeIndex(wide.index)
    return wide.fillna(0.0).sort_index()


def attainment(actual: pd.DataFrame, budget: pd.DataFrame, line: tuple, origin: pd.Timestamp) -> float:
    """Actual over budget for the line's last budgeted months up to `origin`.

    Event lines have months with no budget, so the window widens until it finds some.
    """
    for lookback in (LOOKBACK, 6, 9):
        months = [origin - pd.DateOffset(months=k) for k in range(lookback)]
        months = [m for m in months if m in budget.index and budget.at[m, line] != 0]
        if months:
            ratio = sum(actual.at[m, line] for m in months) / sum(budget.at[m, line] for m in months)
            return float(np.clip(ratio, 0.5, 1.5))
    return 1.0


def holt_winters(history: pd.Series, steps: int) -> np.ndarray:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ExponentialSmoothing(
            history.astype(float),
            trend="add",
            damped_trend=True,
            seasonal="add",
            seasonal_periods=12,
            initialization_method="estimated",
        ).fit(optimized=True)
    # Revenue does not go negative; the event business has months of zero.
    return np.clip(np.asarray(model.forecast(steps), dtype=float), 0.0, None)


def _unit_forecast(method: str, unit: str, actual: pd.DataFrame, budget: pd.DataFrame,
                   origin: pd.Timestamp, months: list[pd.Timestamp]) -> np.ndarray:
    lines = [c for c in budget.columns if c[0] == unit]
    if method == "remaining_budget":
        return np.array([sum(budget.at[m, c] for c in lines) for m in months])
    if method == "plan_attainment":
        ratios = {c: attainment(actual, budget, c, origin) for c in lines}
        return np.array([sum(budget.at[m, c] * ratios[c] for c in lines) for m in months])
    history = actual.loc[:origin, [c for c in actual.columns if c[0] == unit]].sum(axis=1)
    history.index = pd.DatetimeIndex(history.index, freq="MS")
    return holt_winters(history, len(months))


def wape(errors: list[float], actuals: list[float]) -> float:
    total = sum(abs(a) for a in actuals)
    return sum(abs(e) for e in errors) / total if total else 0.0


def backtest(actual: pd.DataFrame, budget: pd.DataFrame, period: date) -> list[dict]:
    end = pd.Timestamp(period)
    units = sorted({c[0] for c in budget.columns})
    scored = {(u, m): ([], []) for u in units + ["all"] for m in METHODS}
    for k in range(ORIGINS, 0, -1):
        origin = end - pd.DateOffset(months=k)
        months = [origin + pd.DateOffset(months=h) for h in range(1, HORIZON + 1)]
        months = [m for m in months if m <= end]
        for unit in units:
            truth = np.array([sum(actual.at[m, c] for c in actual.columns if c[0] == unit) for m in months])
            for method in METHODS:
                forecast = _unit_forecast(method, unit, actual, budget, origin, months)
                for key in (unit, "all"):
                    errs, acts = scored[(key, method)]
                    errs.extend((forecast - truth).tolist())
                    acts.extend(truth.tolist())
    return [{"unit_type": "company" if unit == "all" else "business_unit", "unit_id": unit, "line": "net_revenue",
             "method": method, "mape": round(wape(*scored[(unit, method)]), 4), "origins": ORIGINS,
             "horizon": HORIZON} for unit, method in scored]


def revenue_forecast(actual: pd.DataFrame, budget: pd.DataFrame, period: date) -> list[dict]:
    origin = pd.Timestamp(period)
    months = [m for m in budget.index if m > origin and m.year == period.year]
    if not months:
        return []
    rows, total = [], np.zeros(len(months))
    for unit in sorted({c[0] for c in budget.columns}):
        values = _unit_forecast("plan_attainment", unit, actual, budget, origin, months)
        total += values
        rows += [{"unit_type": "business_unit", "unit_id": unit, "line": "net_revenue", "period": m.date(),
                  "amount": round(float(v), 2), "method": "plan_attainment"}
                 for m, v in zip(months, values, strict=True)]
    rows += [{"unit_type": "company", "unit_id": "all", "line": "net_revenue", "period": m.date(),
              "amount": round(float(v), 2), "method": "plan_attainment"}
             for m, v in zip(months, total, strict=True)]
    return rows


def opex_forecast(conn: psycopg.Connection, catalog: Catalog, period: date) -> list[dict]:
    personnel = catalog.metric_weights("personnel_cost")
    opex = catalog.metric_weights("operating_expenses")
    other = [n for n in opex if n not in personnel and "non_cash" not in catalog.accounts[n].tags]
    rows = conn.execute(
        """
        select cc.department_id,
               coalesce(sum(f.amount) filter (where f.account_number = any(%(pers)s)), 0)::float / 3 as personnel,
               coalesce(sum(f.amount) filter (where f.account_number = any(%(other)s)), 0)::float / 3 as other
        from core.fact_actuals f join core.dim_cost_center cc using (cost_center_id)
        where f.period between %(s)s and %(e)s
        group by 1
        """,
        {"pers": list(personnel), "other": other, "s": add_months(period, -2), "e": period},
    ).fetchall()
    heads = {r["department_id"]: r["heads"] for r in conn.execute(
        """
        select cc.department_id, count(*) as heads
        from core.employee e join core.dim_cost_center cc using (cost_center_id)
        where e.start_date <= %(end)s and (e.term_date is null or e.term_date > %(end)s)
        group by 1
        """,
        {"end": month_end(period)},
    ).fetchall()}
    open_roles = conn.execute(
        "select cc.department_id, r.planned_start from core.requisition r"
        " join core.dim_cost_center cc using (cost_center_id) where r.status = 'open'"
    ).fetchall()
    months, m = [], add_months(period, 1)
    while m.year == period.year:
        months.append(m)
        m = add_months(m, 1)
    out = []
    for r in rows:
        dept = r["department_id"]
        now = max(1, heads.get(dept, 0))
        per_head = r["personnel"] / now
        for month in months:
            # An open role is assumed to start one month after the later of its plan and today.
            expected = sum(1 for o in open_roles if o["department_id"] == dept
                           and add_months(max(o["planned_start"], add_months(period, 1)), 1) <= month)
            out.append({"unit_type": "department", "unit_id": dept, "line": "personnel_cost", "period": month,
                        "amount": round(per_head * (now + expected), 2), "method": "headcount_driven"})
            out.append({"unit_type": "department", "unit_id": dept, "line": "other_opex", "period": month,
                        "amount": round(r["other"], 2), "method": "run_rate"})
    return out


def run(conn: psycopg.Connection, catalog: Catalog, period: date, version: str) -> dict:
    actual = _frame(conn, catalog, "core.fact_actuals", None)
    budget = _frame(conn, catalog, "core.fact_budget", version)
    columns = budget.columns.union(actual.columns)
    actual = actual.reindex(columns=columns, fill_value=0.0)
    budget = budget.reindex(columns=columns, fill_value=0.0)
    scores = backtest(actual, budget, period)
    forecast_rows = revenue_forecast(actual, budget, period) + opex_forecast(conn, catalog, period)
    conn.execute("delete from core.forecast")
    conn.execute("delete from core.forecast_backtest")
    with conn.cursor() as cur:
        cur.executemany(
            "insert into core.forecast (unit_type, unit_id, line, period, amount, method)"
            " values (%(unit_type)s, %(unit_id)s, %(line)s, %(period)s, %(amount)s, %(method)s)",
            forecast_rows,
        )
        cur.executemany(
            "insert into core.forecast_backtest (unit_type, unit_id, line, method, mape, origins, horizon)"
            " values (%(unit_type)s, %(unit_id)s, %(line)s, %(method)s, %(mape)s, %(origins)s, %(horizon)s)",
            scores,
        )
    pooled = {s["method"]: s["mape"] for s in scores if s["unit_type"] == "company"}
    return {"forecast_rows": len(forecast_rows), "wape_pooled": pooled}
