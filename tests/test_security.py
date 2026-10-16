import psycopg
import pytest

from conftest import AUDITOR, CFO, FPA_LEAD, GM_NEWS, NO_GRANTS, PLATFORM_MGR, VP_ENG

RLS_TABLES = ["core.fact_actuals", "core.fact_gl_detail", "core.fact_budget", "core.employee", "core.employee_comp",
              "core.requisition", "core.headcount_exception", "core.forecast", "core.forecast_backtest",
              "core.variance_line"]


def _ccs(conn) -> set[str]:
    return {r["cost_center_id"] for r in conn.execute("select distinct cost_center_id from core.fact_actuals")}


def _units(conn) -> set[tuple[str, str]]:
    rows = conn.execute("select distinct unit_type, unit_id from core.forecast")
    return {(r["unit_type"], r["unit_id"]) for r in rows}


def test_no_identity_means_no_rows(pool):
    with pool.connection() as conn:
        for table in RLS_TABLES:
            assert conn.execute(f"select count(*) as n from {table}").fetchone()["n"] == 0, table


@pytest.mark.parametrize("person", [NO_GRANTS, AUDITOR])
def test_people_without_a_live_grant_see_nothing(as_person, person):
    with as_person(person) as conn:
        for table in RLS_TABLES:
            assert conn.execute(f"select count(*) as n from {table}").fetchone()["n"] == 0, table


def test_cfo_sees_every_cost_center_and_the_balance_sheet(as_person):
    with as_person(CFO) as conn:
        assert len(_ccs(conn) - {None}) == 17
        balance_sheet = "select count(*) as n from core.fact_actuals where cost_center_id is null"
        assert conn.execute(balance_sheet).fetchone()["n"] > 0
        assert conn.execute("select count(*) as n from core.forecast where unit_type = 'company'").fetchone()["n"] > 0


def test_department_head_sees_only_their_department(as_person):
    with as_person(VP_ENG) as conn:
        assert _ccs(conn) == {"4110", "4120", "4130"}
        lines = conn.execute("select department_id, account_number from core.variance_line").fetchall()
        assert {(r["department_id"], r["account_number"]) for r in lines} == {
            ("CORP-ENG", "6010"), ("CORP-ENG", "6050")}
        assert conn.execute("select count(*) as n from core.employee_comp").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from core.employee").fetchone()["n"] > 0
        assert _units(conn) == {("department", "CORP-ENG")}


def test_cost_center_manager_never_sees_a_department_total(as_person):
    with as_person(PLATFORM_MGR) as conn:
        assert _ccs(conn) == {"4110"}
        assert conn.execute("select count(*) as n from core.variance_line").fetchone()["n"] == 0
        assert conn.execute("select count(*) as n from core.forecast").fetchone()["n"] == 0


def test_business_unit_grant_covers_its_departments_and_its_forecast(as_person):
    with as_person(GM_NEWS) as conn:
        assert _ccs(conn) == {"2110", "2120", "2210", "2310"}
        units = _units(conn)
        assert ("business_unit", "LN") in units and ("company", "all") not in units
        depts = {r["department_id"] for r in conn.execute("select department_id from core.variance_line")}
        assert depts == {"LN-SAL"}


def test_salary_needs_the_comp_permission(as_person):
    with as_person(FPA_LEAD) as conn:
        assert conn.execute("select count(*) as n from core.employee_comp").fetchone()["n"] > 400
    with as_person(GM_NEWS) as conn:
        assert conn.execute("select count(*) as n from core.employee_comp").fetchone()["n"] == 0


def test_identity_does_not_leak_between_pooled_requests(pool):
    from fpa.db import scoped

    for _ in range(3):
        with scoped(pool, CFO) as conn:
            assert conn.execute("select count(*) as n from core.fact_actuals").fetchone()["n"] > 0
        with pool.connection() as conn:
            assert conn.execute("select current_setting('app.person_id', true) as p").fetchone()["p"] in (None, "")
            assert conn.execute("select count(*) as n from core.fact_actuals").fetchone()["n"] == 0


def test_api_role_cannot_bypass_or_rewrite_the_rules(as_person):
    attempts = [
        "alter table core.fact_actuals disable row level security",
        "select * from sec.access_grant",
        "select * from ops.audit_log",
        "update core.fact_actuals set amount = 0",
        "insert into sec.access_grant (person_id, scope_type, reason, granted_by) values ('x', 'all', 'r', 'x')",
        "set role fpa",
    ]
    for sql in attempts:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with as_person(VP_ENG) as conn:
                conn.execute(sql)


def test_grant_changes_are_audited(owner):
    with owner.transaction(force_rollback=True):
        owner.execute("select set_config('app.actor', %s, true)", (FPA_LEAD,))
        owner.execute("insert into sec.access_grant (person_id, scope_type, scope_value, reason, granted_by)"
                      " values (%s, 'department', 'CORP-MKT', 'cover for leave', %s)", (VP_ENG, FPA_LEAD))
        row = owner.execute("select actor, action, object, detail from ops.audit_log"
                            " where action = 'grant.insert' order by audit_id desc limit 1").fetchone()
        assert row["actor"] == FPA_LEAD and row["object"] == VP_ENG
        assert row["detail"]["new"]["scope_value"] == "CORP-MKT"


def test_access_review_is_admin_only_and_flags_expired_grants(as_person):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with as_person(VP_ENG) as conn:
            conn.execute("select * from sec.access_review()")
    with as_person(FPA_LEAD) as conn:
        rows = conn.execute("select * from sec.access_review()").fetchall()
    by_person = {r["person_id"]: r for r in rows}
    assert by_person[AUDITOR]["expired"] is True
    assert by_person[VP_ENG]["cost_center_count"] == 3
    assert by_person[NO_GRANTS]["scope_type"] is None
