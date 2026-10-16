import pandas as pd
import pytest

from conftest import CFO, FPA_LEAD, PERIOD, VP_ENG
from fpa import headcount
from fpa.forecast import attainment


def test_plan_attainment_beats_the_remaining_budget_and_the_holt_winters_challenger(owner):
    rows = owner.execute("select unit_id, method, mape::float from core.forecast_backtest"
                         " where unit_type = 'company'").fetchall()
    pooled = {r["method"]: r["mape"] for r in rows}
    assert pooled["plan_attainment"] < pooled["remaining_budget"]
    assert pooled["plan_attainment"] < pooled["holt_winters"]
    assert (pooled["plan_attainment"], pooled["remaining_budget"]) == (0.0146, 0.0229)
    assert pooled["holt_winters"] == pytest.approx(0.0238, abs=0.002)  # optimizer output varies by platform


def test_attainment_widens_its_window_for_event_lines():
    idx = pd.date_range("2026-01-01", "2026-09-01", freq="MS")
    line = ("LE", "4210")
    budget = pd.DataFrame({line: [0, 0, 0, 100, 0, 200, 0, 0, 300]}, index=idx)
    actual = pd.DataFrame({line: [0, 0, 0, 96, 0, 184, 0, 0, 276]}, index=idx)
    assert attainment(actual, budget, line, pd.Timestamp("2026-08-01")) == 0.92
    assert attainment(actual, budget, line, pd.Timestamp("2026-09-01")) == 0.92


def test_forecast_covers_the_rest_of_the_year(owner):
    rows = owner.execute("select unit_type, unit_id, line, period from core.forecast").fetchall()
    revenue = {(r["unit_id"], r["period"].month) for r in rows if r["line"] == "net_revenue"}
    assert revenue == {(u, m) for u in ("CB", "LE", "LN", "all") for m in (10, 11, 12)}
    departments = {r["unit_id"] for r in rows if r["line"] == "personnel_cost"}
    assert len(departments) == 13


def test_engineering_personnel_forecast_rises_as_open_roles_fill(owner):
    rows = owner.execute("select period, amount::float from core.forecast where unit_id = 'CORP-ENG'"
                         " and line = 'personnel_cost' order by period").fetchall()
    oct_, nov, dec = (r["amount"] for r in rows)
    assert oct_ < nov < dec


def test_payroll_hris_exceptions(owner):
    found = headcount.find_exceptions(owner, PERIOD)
    assert sorted((x["kind"], x["cost_center_id"]) for x in found) == [
        ("active_not_paid", "1120"), ("cost_center_mismatch", "4110"), ("paid_not_active", "2120")]
    assert all("$" not in x["detail"] for x in found)


def test_headcount_report_respects_scope_and_comp_permission(as_person):
    with as_person(VP_ENG) as conn:
        report = headcount.report(conn, PERIOD, "FY26 Budget")
    assert {r["department_id"] for r in report["by_cost_center"]} == {"CORP-ENG"}
    platform = next(r for r in report["by_cost_center"] if r["cost_center_id"] == "4110")
    assert platform["open_roles"] == 4
    assert report["salary_visible"] is False
    assert [x["kind"] for x in report["exceptions"]] == ["cost_center_mismatch"]
    with as_person(FPA_LEAD) as conn:
        assert headcount.report(conn, PERIOD, "FY26 Budget")["salary_visible"] is True
    with as_person(CFO) as conn:
        everyone = headcount.report(conn, PERIOD, "FY26 Budget")
    assert sum(r["actual"] for r in everyone["by_cost_center"]) == 458
