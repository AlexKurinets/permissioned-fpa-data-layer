-- Tables for the four layers: src (extracts as the adapters land them), core (the
-- published finance layer people and AI tools read), sec (who may see what) and
-- ops (refresh runs, tie-out results and the audit log).

create schema if not exists src;
create schema if not exists core;
create schema if not exists sec;
create schema if not exists ops;

-- src: written by the adapters, never read by the API --------------------------------

create table src.erp_entities (
    entity_id text primary key,
    name text not null,
    currency text not null
);

create table src.erp_cost_centers (
    cost_center_id text primary key,
    name text not null,
    department_id text not null,
    department_name text not null,
    business_unit_id text not null,
    business_unit_name text not null,
    entity_id text not null references src.erp_entities
);

create table src.erp_accounts (
    account_number text primary key,
    name text not null,
    account_type text not null
);

-- GL detail exactly as the ERP exports it: debits positive, credits negative.
create table src.erp_gl_lines (
    line_id bigint primary key,
    journal_id text not null,
    entity_id text not null,
    period date not null,
    posted_on date not null,
    account_number text not null,
    cost_center_id text,
    vendor text,
    memo text,
    amount numeric(16, 2) not null
);
create index erp_gl_lines_period on src.erp_gl_lines (period, entity_id);

-- The ERP's own trial balance activity, used as control totals for the tie-out.
create table src.erp_trial_balance (
    entity_id text not null,
    period date not null,
    account_number text not null,
    net_activity numeric(16, 2) not null,
    primary key (entity_id, period, account_number)
);

-- Budget from the planning workbook, already in reporting sign.
create table src.plan_budget (
    version text not null,
    period date not null,
    cost_center_id text not null,
    account_number text not null,
    amount numeric(16, 2) not null,
    primary key (version, period, cost_center_id, account_number)
);

-- Headcount plan (FTE) from the same planning workbook.
create table src.plan_headcount (
    version text not null,
    period date not null,
    cost_center_id text not null,
    fte numeric(8, 2) not null,
    primary key (version, period, cost_center_id)
);

create table src.hris_employees (
    employee_id text primary key,
    full_name text not null,
    cost_center_id text not null,
    title text not null,
    level text not null,
    start_date date not null,
    term_date date,
    annual_salary numeric(12, 2) not null
);

create table src.hris_requisitions (
    req_id text primary key,
    cost_center_id text not null,
    title text not null,
    level text not null,
    planned_start date not null,
    budgeted_salary numeric(12, 2) not null,
    status text not null check (status in ('open', 'filled')),
    filled_by text
);

create table src.payroll_register (
    period date not null,
    employee_id text not null,
    cost_center_id text not null,
    gross_pay numeric(12, 2) not null,
    primary key (period, employee_id)
);

-- Lines removed by `fpa simulate drop-line`, kept so `fpa simulate restore` can put them back.
create table src.dropped_lines (like src.erp_gl_lines);

-- core: the published finance layer ---------------------------------------------------

create table core.dim_entity (
    entity_id text primary key,
    name text not null,
    currency text not null
);

create table core.dim_cost_center (
    cost_center_id text primary key,
    name text not null,
    department_id text not null,
    department_name text not null,
    business_unit_id text not null,
    business_unit_name text not null,
    entity_id text not null
);

create table core.dim_account (
    account_number text primary key,
    name text not null,
    section text not null,
    grp text not null,
    statement text not null,
    nature text not null,
    reporting_sign smallint not null,
    favorable_when text,
    budgeted boolean not null,
    tags text[] not null default '{}',
    definition text not null,
    nuances text[] not null default '{}',
    synonyms text[] not null default '{}',
    owner text not null
);

-- Monthly actuals in reporting sign: revenue and costs positive, contra accounts negative.
create table core.fact_actuals (
    period date not null,
    entity_id text not null,
    cost_center_id text,
    account_number text not null,
    amount numeric(16, 2) not null
);
create index fact_actuals_cc on core.fact_actuals (cost_center_id, period);

-- Line-level P&L detail for drill-down, in reporting sign.
create table core.fact_gl_detail (
    line_id bigint primary key,
    period date not null,
    entity_id text not null,
    cost_center_id text not null,
    account_number text not null,
    vendor text,
    memo text,
    amount numeric(16, 2) not null
);
create index fact_gl_detail_cc on core.fact_gl_detail (cost_center_id, period, account_number);

create table core.fact_budget (
    version text not null,
    period date not null,
    cost_center_id text not null,
    account_number text not null,
    amount numeric(16, 2) not null,
    primary key (version, period, cost_center_id, account_number)
);

create table core.fact_budget_headcount (
    version text not null,
    period date not null,
    cost_center_id text not null,
    fte numeric(8, 2) not null,
    primary key (version, period, cost_center_id)
);

create table core.employee (
    employee_id text primary key,
    full_name text not null,
    cost_center_id text not null,
    title text not null,
    level text not null,
    start_date date not null,
    term_date date
);

-- Salary lives in its own table so that it can carry its own policy.
create table core.employee_comp (
    employee_id text primary key,
    cost_center_id text not null,
    annual_salary numeric(12, 2) not null
);

create table core.requisition (
    req_id text primary key,
    cost_center_id text not null,
    title text not null,
    level text not null,
    planned_start date not null,
    budgeted_salary numeric(12, 2) not null,
    status text not null,
    filled_by text
);

create table core.headcount_exception (
    period date not null,
    employee_id text not null,
    cost_center_id text not null,
    kind text not null,
    detail text not null
);

-- Forecasts and backtests are kept at the unit they were modelled at.
create table core.forecast (
    unit_type text not null check (unit_type in ('company', 'business_unit', 'department')),
    unit_id text not null,
    line text not null,
    period date not null,
    amount numeric(16, 2) not null,
    method text not null,
    primary key (unit_type, unit_id, line, period)
);

create table core.forecast_backtest (
    unit_type text not null,
    unit_id text not null,
    line text not null,
    method text not null,
    mape numeric(8, 4) not null,
    origins integer not null,
    horizon integer not null,
    primary key (unit_type, unit_id, line, method)
);

create table core.variance_line (
    variance_id bigserial primary key,
    period date not null,
    department_id text not null,
    account_number text not null,
    actual numeric(16, 2) not null,
    budget numeric(16, 2) not null,
    variance numeric(16, 2) not null,
    variance_pct numeric(10, 4),
    favorable boolean not null,
    driver text not null,
    facts jsonb not null,
    draft text not null,
    draft_source text not null,
    validator_note text,
    status text not null default 'draft' check (status in ('draft', 'accepted', 'edited', 'rejected')),
    final_text text,
    reviewed_by text,
    reviewed_at timestamptz,
    review_note text,
    unique (period, department_id, account_number)
);

-- sec: identity and grants -----------------------------------------------------------

create table sec.person (
    person_id text primary key,          -- the IdP's email claim
    full_name text not null,
    title text not null,
    active boolean not null default true
);

-- Group membership as synced from the identity provider.
create table sec.person_group (
    person_id text not null references sec.person,
    group_name text not null,
    primary key (person_id, group_name)
);

create table sec.group_permission (
    group_name text not null,
    permission text not null check (permission in ('review_variances', 'comp_detail', 'admin_access')),
    primary key (group_name, permission)
);

-- Data scope is granted per person, not per group, and every grant says why.
create table sec.access_grant (
    grant_id bigserial primary key,
    person_id text not null references sec.person,
    scope_type text not null check (scope_type in ('all', 'business_unit', 'department', 'cost_center')),
    scope_value text,
    reason text not null,
    granted_by text not null,
    granted_at timestamptz not null default now(),
    expires_at timestamptz,
    check ((scope_type = 'all') = (scope_value is null))
);

-- ops: runs, tie-out and audit -------------------------------------------------------

create table ops.refresh_run (
    run_id text primary key,
    period date not null,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null check (status in ('running', 'published', 'failed_tieout', 'failed_coverage', 'failed')),
    summary jsonb not null default '{}'
);

create table ops.tieout_break (
    run_id text not null references ops.refresh_run,
    entity_id text not null,
    period date not null,
    account_number text,
    check_name text not null,
    warehouse_amount numeric(16, 2),
    erp_amount numeric(16, 2),
    difference numeric(16, 2)
);

create table ops.audit_log (
    audit_id bigserial primary key,
    at timestamptz not null default now(),
    actor text,
    action text not null,
    object text,
    detail jsonb not null default '{}'
);
