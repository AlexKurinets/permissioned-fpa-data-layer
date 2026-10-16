"""First-pass budget-vs-actual: materiality, driver facts and commentary drafts.

The numbers and the drivers are computed here, in SQL and Python, before any model is
involved. The drafter (template or Claude) only turns those facts into a sentence, and
the validator rejects any draft containing a number that is not in the facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

from fpa.semantic import Catalog, is_favorable
from fpa.synthetic import add_months


def money(value: float) -> str:
    """Dollar display used everywhere: rounded to the nearest hundred."""
    rounded = round(abs(value), -2)
    return f"${rounded:,.0f}"


def pct(value: float) -> str:
    return f"{abs(value):.1f}%"


def month_label(d: date) -> str:
    return d.strftime("%B %Y")


@dataclass
class Line:
    department_id: str
    department_name: str
    account_number: str
    actual: float
    budget: float

    @property
    def variance(self) -> float:
        return self.actual - self.budget

    @property
    def variance_pct(self) -> float | None:
        return self.variance / abs(self.budget) * 100 if self.budget else None


def department_lines(conn: psycopg.Connection, catalog: Catalog, period: date, version: str) -> list[Line]:
    rows = conn.execute(
        """
        with a as (
            select cc.department_id, f.account_number, sum(f.amount) as amount
            from core.fact_actuals f join core.dim_cost_center cc using (cost_center_id)
            where f.period = %(p)s group by 1, 2),
        b as (
            select cc.department_id, f.account_number, sum(f.amount) as amount
            from core.fact_budget f join core.dim_cost_center cc using (cost_center_id)
            where f.period = %(p)s and f.version = %(v)s group by 1, 2),
        d as (select distinct department_id, department_name from core.dim_cost_center)
        select coalesce(a.department_id, b.department_id) as department_id, d.department_name,
               coalesce(a.account_number, b.account_number) as account_number,
               coalesce(a.amount, 0)::float as actual, coalesce(b.amount, 0)::float as budget
        from a full join b on a.department_id = b.department_id and a.account_number = b.account_number
        join d on d.department_id = coalesce(a.department_id, b.department_id)
        """,
        {"p": period, "v": version},
    ).fetchall()
    out = []
    for r in rows:
        account = catalog.accounts[r["account_number"]]
        if not account.budgeted or "eliminate_on_consolidation" in account.tags:
            continue
        out.append(Line(r["department_id"], r["department_name"], r["account_number"], r["actual"], r["budget"]))
    return out


def is_material(catalog: Catalog, line: Line) -> bool:
    if abs(line.variance) < catalog.min_abs_variance:
        return False
    return line.variance_pct is None or abs(line.variance_pct) >= catalog.min_pct_variance * 100


def _amount(conn, department_id: str, account: str, start: date, end: date, table: str, version: str | None):
    sql = (f"select coalesce(sum(f.amount), 0)::float as v from {table} f "
           "join core.dim_cost_center cc using (cost_center_id) "
           "where cc.department_id = %s and f.account_number = %s and f.period between %s and %s")
    params: list = [department_id, account, start, end]
    if version:
        sql += " and f.version = %s"
        params.append(version)
    return conn.execute(sql, params).fetchone()["v"]


def build_facts(conn: psycopg.Connection, catalog: Catalog, line: Line, period: date, version: str) -> dict:
    account = catalog.accounts[line.account_number]
    revenue_like = account.favorable_when == "above_budget"
    actual = lambda s, e: _amount(conn, line.department_id, line.account_number, s, e, "core.fact_actuals", None)
    planned = lambda s, e: _amount(conn, line.department_id, line.account_number, s, e, "core.fact_budget", version)

    favorable = is_favorable(account.favorable_when, line.variance)
    if revenue_like:
        direction = "above budget" if line.variance >= 0 else "below budget"
    else:
        direction = "over budget" if line.variance >= 0 else "under budget"
    facts: dict = {
        "period": period.strftime("%Y-%m"),
        "period_label": month_label(period),
        "department": line.department_name,
        "account_number": account.number,
        "account_name": account.name,
        "account_definition": account.definition,
        "account_nuances": list(account.nuances),
        "actual": round(line.actual, 2),
        "budget": round(line.budget, 2),
        "variance": round(line.variance, 2),
        "variance_pct": round(line.variance_pct, 1) if line.variance_pct is not None else None,
        "favorable": favorable,
        "direction": direction,
        "display": {"actual": money(line.actual), "budget": money(line.budget),
                    "variance": money(line.variance),
                    "variance_pct": pct(line.variance_pct) if line.variance_pct is not None else None},
    }

    year_start = date(period.year, 1, 1)
    ytd_a, ytd_b = actual(year_start, period), planned(year_start, period)
    facts["ytd"] = {"actual": round(ytd_a, 2), "budget": round(ytd_b, 2), "variance": round(ytd_a - ytd_b, 2),
                    "display_variance": money(ytd_a - ytd_b),
                    "direction": ("over" if ytd_a - ytd_b >= 0 else "under")}

    prev = add_months(period, -1)
    if prev.year == period.year:
        pa, pb = actual(prev, prev), planned(prev, prev)
        facts["prior_month"] = {"label": month_label(prev), "actual": round(pa, 2), "budget": round(pb, 2),
                                "variance": round(pa - pb, 2), "display_variance": money(pa - pb),
                                "direction": "over" if pa - pb >= 0 else "under"}

    last_year = date(period.year - 1, period.month, 1)
    ly = actual(last_year, last_year)
    facts["same_month_last_year"] = {"label": month_label(last_year), "actual": round(ly, 2),
                                     "display_actual": money(ly)}
    if ly:
        facts["same_month_last_year"]["change_pct"] = round((line.actual - ly) / abs(ly) * 100, 1)

    trend = []
    for back in (3, 2, 1):
        m = add_months(period, -back)
        if m.year != period.year:
            continue
        ma, mb = actual(m, m), planned(m, m)
        if mb:
            trend.append({"label": month_label(m), "variance_pct": round((ma - mb) / abs(mb) * 100, 1)})
    facts["trend"] = trend

    if "vendor_detail" in account.tags:
        facts["vendors"] = _vendor_facts(conn, line, period)
    if account.group == "Personnel":
        facts["headcount"] = _headcount_facts(conn, line.department_id, period)

    facts["driver"] = classify(facts, account.section.name)
    return facts


def _vendor_facts(conn, line: Line, period: date) -> list[dict]:
    rows = conn.execute(
        """
        with lines as (
            select d.vendor, d.period, sum(d.amount)::float as amount
            from core.fact_gl_detail d join core.dim_cost_center cc using (cost_center_id)
            where cc.department_id = %(dept)s and d.account_number = %(acct)s and d.vendor is not null
            group by 1, 2)
        select vendor,
               sum(amount) filter (where period = %(p)s) as amount,
               coalesce(sum(amount) filter (where period between %(t0)s and %(t1)s), 0) / 3 as trailing_3m_avg,
               coalesce(sum(amount) filter (where period = %(ly)s), 0) as same_month_last_year,
               min(period) as first_seen
        from lines group by vendor
        having sum(amount) filter (where period = %(p)s) is not null
        order by abs(sum(amount) filter (where period = %(p)s)
                     - coalesce(sum(amount) filter (where period = %(ly)s), 0)) desc
        limit 3
        """,
        {"dept": line.department_id, "acct": line.account_number, "p": period,
         "t0": add_months(period, -3), "t1": add_months(period, -1),
         "ly": date(period.year - 1, period.month, 1)},
    ).fetchall()
    out = []
    for r in rows:
        first_seen = r["first_seen"]
        out.append({
            "vendor": r["vendor"],
            "amount": round(r["amount"], 2),
            "display_amount": money(r["amount"]),
            "trailing_3m_avg": round(r["trailing_3m_avg"], 2),
            "same_month_last_year": round(r["same_month_last_year"], 2),
            "change_vs_last_year": round(r["amount"] - r["same_month_last_year"], 2),
            "display_change_vs_last_year": money(r["amount"] - r["same_month_last_year"]),
            "first_seen": month_label(first_seen),
            "new_in_last_6_months": first_seen > add_months(period, -6),
        })
    return out


def _headcount_facts(conn, department_id: str, period: date) -> dict:
    open_roles = conn.execute(
        """
        select r.title, cc.name as cost_center, r.planned_start, r.budgeted_salary::float as budgeted_salary
        from core.requisition r join core.dim_cost_center cc using (cost_center_id)
        where cc.department_id = %s and r.status = 'open' and r.planned_start <= %s
        order by r.planned_start, r.req_id
        """,
        (department_id, period),
    ).fetchall()
    monthly = sum(r["budgeted_salary"] for r in open_roles) / 12
    starts = [r["planned_start"] for r in open_roles]
    return {
        "open_role_count": len(open_roles),
        "open_roles": [{"title": r["title"], "cost_center": r["cost_center"],
                        "planned_start": month_label(r["planned_start"])} for r in open_roles],
        "open_role_cost_centers": sorted({r["cost_center"] for r in open_roles}),
        "first_planned_start": month_label(min(starts)) if starts else None,
        "last_planned_start": month_label(max(starts)) if starts else None,
        "open_role_monthly_salary": round(monthly, 2),
        "display_open_role_monthly_salary": money(monthly),
    }


def classify(facts: dict, section: str) -> str:
    variance = facts["variance"]
    prior = facts.get("prior_month")
    if prior and prior["variance"] * variance < 0 and abs(prior["variance"]) >= 0.5 * abs(variance) \
            and abs(facts["ytd"]["variance"]) < 0.5 * abs(variance):
        return "timing"
    vendors = facts.get("vendors") or []
    new_spend = sum(v["amount"] for v in vendors if v["new_in_last_6_months"])
    if vendors and variance > 0 and new_spend >= 0.5 * variance:
        return "new_vendor"
    if vendors and vendors[0]["change_vs_last_year"] * variance > 0 \
            and abs(vendors[0]["change_vs_last_year"]) >= 0.5 * abs(variance):
        return "vendor"
    if (facts.get("headcount") or {}).get("open_role_count"):
        return "headcount"
    if section == "Revenue":
        return "volume"
    return "other"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def template_draft(facts: dict) -> str:
    """The deterministic first pass. Every number comes from facts['display'] or the driver facts."""
    d = facts["display"]
    head = f"The {facts['account_name']} line in {facts['department']} was {d['variance']} {facts['direction']}"
    head += f" ({d['variance_pct']})" if d["variance_pct"] else ""
    head += f" in {facts['period_label']}, at {d['actual']} against a budget of {d['budget']}."
    parts = [head]
    driver = facts["driver"]
    if driver == "timing":
        p, y = facts["prior_month"], facts["ytd"]
        parts.append(f"{p['label']} was {p['display_variance']} {p['direction']} budget on the same line, "
                     f"so year to date it is {y['display_variance']} {y['direction']}, which points to timing.")
    elif driver == "new_vendor":
        v = next(v for v in facts["vendors"] if v["new_in_last_6_months"])
        parts.append(f"{v['vendor']}, first billed in {v['first_seen']}, accounts for {v['display_amount']} "
                     "of this month's spend.")
    elif driver == "vendor":
        v = facts["vendors"][0]
        more = "more" if v["change_vs_last_year"] >= 0 else "less"
        parts.append(f"{v['vendor']} billed {v['display_amount']}, {v['display_change_vs_last_year']} {more} "
                     f"than in {facts['same_month_last_year']['label']}.")
    elif driver == "volume":
        ly = facts["same_month_last_year"]
        if "change_pct" in ly:
            word = "above" if ly["change_pct"] >= 0 else "below"
            parts.append(f"The line is {pct(ly['change_pct'])} {word} {ly['label']}.")
        # The unbroken run of earlier budgeted months on the same side of budget, most recent first.
        run = []
        for t in reversed(facts["trend"]):
            if t["variance_pct"] * facts["variance"] <= 0:
                break
            run.append(t)
        if len(run) == 1:
            t = run[0]
            side = "above" if t["variance_pct"] > 0 else "below"
            parts.append(f"{t['label']} was also {pct(t['variance_pct'])} {side} budget.")
        elif run:
            first, last = run[-1], run[0]
            side = "above" if first["variance_pct"] > 0 else "below"
            parts.append(f"It has been {side} budget every month since {first['label']}, "
                         f"moving from {pct(first['variance_pct'])} to {pct(last['variance_pct'])} in {last['label']}.")
    hc = facts.get("headcount") or {}
    if hc.get("open_role_count"):
        n = hc["open_role_count"]
        roles = "role" if n == 1 else "roles"
        window = (hc["first_planned_start"] if hc["first_planned_start"] == hc["last_planned_start"]
                  else f"between {hc['first_planned_start']} and {hc['last_planned_start']}")
        lead = "Separately, the" if driver != "headcount" else "The"
        parts.append(f"{lead} budget includes {n} {_join(hc['open_role_cost_centers'])} {roles} planned to start "
                     f"{window} that {'is' if n == 1 else 'are'} still open, worth about "
                     f"{hc['display_open_role_monthly_salary']} a month in salary.")
    return " ".join(parts)
