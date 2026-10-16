"""Loads the synthetic source systems and the people who use the platform."""

from __future__ import annotations

import psycopg

from fpa import synthetic

SOURCE_TABLES = {
    "src.erp_entities": ("entity_id", "name", "currency"),
    "src.erp_cost_centers": ("cost_center_id", "name", "department_id", "department_name",
                             "business_unit_id", "business_unit_name", "entity_id"),
    "src.erp_accounts": ("account_number", "name", "account_type"),
    "src.erp_gl_lines": ("line_id", "journal_id", "entity_id", "period", "posted_on", "account_number",
                         "cost_center_id", "vendor", "memo", "amount"),
    "src.erp_trial_balance": ("entity_id", "period", "account_number", "net_activity"),
    "src.plan_budget": ("version", "period", "cost_center_id", "account_number", "amount"),
    "src.plan_headcount": ("version", "period", "cost_center_id", "fte"),
    "src.hris_employees": ("employee_id", "full_name", "cost_center_id", "title", "level", "start_date",
                           "term_date", "annual_salary"),
    "src.hris_requisitions": ("req_id", "cost_center_id", "title", "level", "planned_start",
                              "budgeted_salary", "status", "filled_by"),
    "src.payroll_register": ("period", "employee_id", "cost_center_id", "gross_pay"),
}


def _copy(conn: psycopg.Connection, table: str, columns: tuple[str, ...], rows) -> int:
    count = 0
    with conn.cursor() as cur:
        with cur.copy(f"copy {table} ({', '.join(columns)}) from stdin") as copy:
            for row in rows:
                copy.write_row(row)
                count += 1
    return count


def write_sources(conn: psycopg.Connection, data: synthetic.SourceData) -> dict[str, int]:
    payload = {
        "src.erp_entities": data.entities,
        "src.erp_cost_centers": data.cost_centers,
        "src.erp_accounts": data.accounts,
        "src.erp_gl_lines": data.gl_lines,
        "src.erp_trial_balance": data.trial_balance,
        "src.plan_budget": data.budget,
        "src.plan_headcount": data.plan_headcount,
        "src.hris_employees": data.employees,
        "src.hris_requisitions": data.requisitions,
        "src.payroll_register": data.payroll,
    }
    counts = {}
    with conn.transaction():
        conn.execute("truncate src.dropped_lines, " + ", ".join(reversed(list(SOURCE_TABLES))) + " cascade")
        for table, columns in SOURCE_TABLES.items():
            counts[table] = _copy(conn, table, columns, payload[table])
    return counts


def write_people(conn: psycopg.Connection) -> int:
    with conn.transaction():
        conn.execute("select set_config('app.actor', 'seed', true)")
        conn.execute("delete from sec.access_grant")
        conn.execute("delete from sec.person_group")
        conn.execute("delete from sec.group_permission")
        conn.execute("delete from sec.person")
        for group, permission in synthetic.GROUP_PERMISSIONS:
            conn.execute("insert into sec.group_permission values (%s, %s)", (group, permission))
        for person_id, name, title, groups, grants in synthetic.PEOPLE:
            conn.execute("insert into sec.person (person_id, full_name, title) values (%s, %s, %s)",
                         (person_id, name, title))
            for group in groups:
                conn.execute("insert into sec.person_group values (%s, %s)", (person_id, group))
            for scope_type, scope_value, reason, expires_at in grants:
                conn.execute(
                    "insert into sec.access_grant (person_id, scope_type, scope_value, reason,"
                    " granted_by, granted_at, expires_at) values (%s, %s, %s, %s, %s, '2026-01-05', %s)",
                    (person_id, scope_type, scope_value, reason, synthetic.GRANTOR, expires_at),
                )
    return len(synthetic.PEOPLE)


def seed(conn: psycopg.Connection, seed_value: int = synthetic.SEED) -> dict[str, int]:
    counts = write_sources(conn, synthetic.generate(seed_value))
    counts["sec.person"] = write_people(conn)
    return counts


def is_seeded(conn: psycopg.Connection) -> bool:
    return conn.execute("select exists (select 1 from src.erp_gl_lines) as ok").fetchone()["ok"]
