"""Headcount: plan versus actual, and the payroll-versus-HRIS reconciliation."""

from __future__ import annotations

from datetime import date

import psycopg

from fpa.synthetic import month_end


def _day(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def find_exceptions(conn: psycopg.Connection, period: date) -> list[dict]:
    """People the HRIS and the payroll run disagree about for one period.

    Runs against the source extracts as the pipeline owner. Details never include pay
    amounts, because budget owners can see exceptions but not individual pay.
    """
    start, end = period, month_end(period)
    label = f"{period:%B %Y}"
    hris = {r["employee_id"]: r for r in conn.execute(
        "select employee_id, cost_center_id, start_date, term_date from src.hris_employees")}
    paid = {r["employee_id"]: r for r in conn.execute(
        "select employee_id, cost_center_id from src.payroll_register where period = %s", (period,))}
    out = []
    for emp, h in hris.items():
        active = h["start_date"] <= end and (h["term_date"] is None or h["term_date"] >= start)
        if active and emp not in paid:
            out.append({"employee_id": emp, "cost_center_id": h["cost_center_id"], "kind": "active_not_paid",
                        "detail": f"Active in HRIS from {_day(h['start_date'])} but missing from the "
                                  f"{label} payroll run."})
        if emp in paid and not active:
            when = (f"a termination date of {_day(h['term_date'])}" if h["term_date"] and h["term_date"] < start
                    else f"a start date of {_day(h['start_date'])}")
            out.append({"employee_id": emp, "cost_center_id": h["cost_center_id"], "kind": "paid_not_active",
                        "detail": f"Included in the {label} payroll run, but HRIS shows {when}."})
        if active and emp in paid and paid[emp]["cost_center_id"] != h["cost_center_id"]:
            out.append({"employee_id": emp, "cost_center_id": h["cost_center_id"], "kind": "cost_center_mismatch",
                        "detail": f"HRIS has cost center {h['cost_center_id']}, but payroll charged "
                                  f"{paid[emp]['cost_center_id']} in {label}."})
    for emp, p in paid.items():
        if emp not in hris:
            out.append({"employee_id": emp, "cost_center_id": p["cost_center_id"], "kind": "paid_not_in_hris",
                        "detail": f"Included in the {label} payroll run but has no HRIS record."})
    return sorted(out, key=lambda r: (r["kind"], r["employee_id"]))


def report(conn: psycopg.Connection, period: date, version: str) -> dict:
    """Headcount for whatever the caller may see. Runs under row-level security.

    Plan is the planning workbook's FTE plan for the month: budgeted positions, including
    new roles from their planned start, less the vacancy allowance.
    """
    end = month_end(period)
    year_start = date(period.year - 1, 12, 31)
    by_cc = conn.execute(
        """
        with emp as (
            select cost_center_id,
                   count(*) filter (where start_date <= %(end)s
                                    and (term_date is null or term_date > %(end)s)) as actual,
                   count(*) filter (where start_date <= %(ys)s
                                    and (term_date is null or term_date > %(ys)s)) as start_of_year,
                   count(*) filter (where start_date > %(ys)s and start_date <= %(end)s) as hires_ytd,
                   count(*) filter (where term_date > %(ys)s and term_date <= %(end)s) as departures_ytd
            from core.employee group by 1),
        req as (
            select cost_center_id, count(*) filter (where planned_start <= %(p)s and status = 'open') as open_roles
            from core.requisition group by 1),
        plan as (
            select cost_center_id, fte from core.fact_budget_headcount where period = %(p)s and version = %(v)s)
        select cc.cost_center_id, cc.name as cost_center, cc.department_id, cc.department_name,
               cc.business_unit_name, coalesce(emp.actual, 0) as actual,
               coalesce(plan.fte, 0)::float as plan,
               coalesce(req.open_roles, 0) as open_roles, coalesce(emp.hires_ytd, 0) as hires_ytd,
               coalesce(emp.departures_ytd, 0) as departures_ytd
        from core.dim_cost_center cc
        join emp using (cost_center_id)
        left join req using (cost_center_id)
        left join plan using (cost_center_id)
        order by cc.cost_center_id
        """,
        {"end": end, "ys": year_start, "p": period, "v": version},
    ).fetchall()
    open_roles = conn.execute(
        """
        select r.req_id, r.title, r.level, cc.name as cost_center, cc.department_name, r.planned_start
        from core.requisition r join core.dim_cost_center cc using (cost_center_id)
        where r.status = 'open' and r.planned_start <= %s
        order by r.planned_start, r.req_id
        """,
        (period,),
    ).fetchall()
    exceptions = conn.execute(
        """
        select x.kind, x.employee_id, e.full_name, cc.name as cost_center, x.detail
        from core.headcount_exception x
        left join core.employee e using (employee_id)
        join core.dim_cost_center cc on cc.cost_center_id = x.cost_center_id
        where x.period = %s
        order by x.kind, x.employee_id
        """,
        (period,),
    ).fetchall()
    people = conn.execute(
        """
        select e.employee_id, e.full_name, e.title, e.level, cc.name as cost_center, e.start_date,
               c.annual_salary::float as annual_salary
        from core.employee e
        join core.dim_cost_center cc using (cost_center_id)
        left join core.employee_comp c using (employee_id)
        where e.start_date <= %(end)s and (e.term_date is null or e.term_date > %(end)s)
        order by cc.cost_center_id, e.level desc, e.full_name
        """,
        {"end": end},
    ).fetchall()
    comp_visible = any(p["annual_salary"] is not None for p in people)
    return {
        "period": period.isoformat(),
        "by_cost_center": by_cc,
        "open_roles": open_roles,
        "exceptions": exceptions,
        "employees": people,
        "salary_visible": comp_visible,
    }
