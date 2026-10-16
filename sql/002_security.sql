-- Person-level access, enforced by Postgres row-level security.
--
-- The API connects as fpa_app, a role that owns nothing, and sets app.person_id from
-- the verified identity token at the start of every transaction. Every policy below
-- applies to fpa_app, so the API, the dashboard, the package builder and the question
-- answerer all get the same answer for the same person. The pipeline connects as the
-- owner, which RLS does not restrict (enabled, deliberately not forced).
--
-- A person with no grant sees nothing: there is no default scope.

create or replace function sec.current_person() returns text
language sql stable as $$
    select nullif(current_setting('app.person_id', true), '')
$$;

create or replace function sec.active_grants() returns setof sec.access_grant
language sql stable security definer set search_path = pg_catalog, sec as $$
    select g.*
    from sec.access_grant g
    join sec.person p on p.person_id = g.person_id and p.active
    where g.person_id = sec.current_person()
      and (g.expires_at is null or g.expires_at > now())
$$;

-- Grants resolve down the hierarchy: a business unit grant covers its departments,
-- a department grant covers its cost centers.
create or replace function sec.visible_cost_centers() returns text[]
language sql stable security definer set search_path = pg_catalog, sec, core as $$
    select coalesce(array_agg(distinct cc.cost_center_id order by cc.cost_center_id), '{}')
    from sec.active_grants() g
    join core.dim_cost_center cc
      on g.scope_type = 'all'
      or (g.scope_type = 'business_unit' and cc.business_unit_id = g.scope_value)
      or (g.scope_type = 'department' and cc.department_id = g.scope_value)
      or (g.scope_type = 'cost_center' and cc.cost_center_id = g.scope_value)
$$;

create or replace function sec.sees_all() returns boolean
language sql stable security definer set search_path = pg_catalog, sec as $$
    select exists (select 1 from sec.active_grants() where scope_type = 'all')
$$;

-- A department, business unit or the company is visible only when every one of its
-- cost centers is. Aggregates are therefore never shown to someone who could subtract
-- the parts they can see from the total to recover the parts they cannot.
create or replace function sec.visible_units() returns text[]
language sql stable security definer set search_path = pg_catalog, sec, core as $$
    with visible as (
        select unnest(sec.visible_cost_centers()) as cost_center_id
    ),
    units as (
        select 'department' as unit_type, department_id as unit_id, cost_center_id from core.dim_cost_center
        union all
        select 'business_unit', business_unit_id, cost_center_id from core.dim_cost_center
        union all
        select 'company', 'all', cost_center_id from core.dim_cost_center
    )
    select coalesce(array_agg(unit_type || ':' || unit_id order by unit_type, unit_id), '{}')
    from (
        select u.unit_type, u.unit_id
        from units u
        left join visible v on v.cost_center_id = u.cost_center_id
        group by u.unit_type, u.unit_id
        having bool_and(v.cost_center_id is not null)
    ) fully_visible
$$;

create or replace function sec.has_permission(wanted text) returns boolean
language sql stable security definer set search_path = pg_catalog, sec as $$
    select exists (
        select 1
        from sec.person_group pg
        join sec.group_permission gp on gp.group_name = pg.group_name
        join sec.person p on p.person_id = pg.person_id and p.active
        where pg.person_id = sec.current_person()
          and gp.permission = wanted
    )
$$;

create or replace function sec.whoami()
returns table (person_id text, full_name text, title text, permissions text[], grants jsonb,
               cost_centers text[], units text[])
language sql stable security definer set search_path = pg_catalog, sec, core as $$
    select p.person_id, p.full_name, p.title,
           coalesce((select array_agg(distinct gp.permission order by gp.permission)
                     from sec.person_group pg
                     join sec.group_permission gp on gp.group_name = pg.group_name
                     where pg.person_id = p.person_id), '{}'),
           coalesce((select jsonb_agg(jsonb_build_object(
                         'scope_type', g.scope_type, 'scope_value', g.scope_value,
                         'reason', g.reason, 'expires_at', g.expires_at) order by g.grant_id)
                     from sec.active_grants() g), '[]'::jsonb),
           sec.visible_cost_centers(),
           sec.visible_units()
    from sec.person p
    where p.person_id = sec.current_person() and p.active
$$;

-- Who can see what, for the quarterly access review. Admins only.
create or replace function sec.access_review()
returns table (person_id text, full_name text, title text, active boolean, scope_type text,
               scope_value text, cost_center_count integer, permissions text[], reason text,
               granted_by text, granted_at timestamptz, expires_at timestamptz, expired boolean)
language plpgsql stable security definer set search_path = pg_catalog, sec, core as $$
#variable_conflict use_column
begin
    if not sec.has_permission('admin_access') then
        raise exception 'the access review requires the admin_access permission'
            using errcode = '42501';
    end if;
    return query
    select p.person_id, p.full_name, p.title, p.active, g.scope_type, g.scope_value,
           (select count(*)::integer from core.dim_cost_center cc
             where g.scope_type = 'all'
                or (g.scope_type = 'business_unit' and cc.business_unit_id = g.scope_value)
                or (g.scope_type = 'department' and cc.department_id = g.scope_value)
                or (g.scope_type = 'cost_center' and cc.cost_center_id = g.scope_value)),
           coalesce((select array_agg(distinct gp.permission order by gp.permission)
                     from sec.person_group pg
                     join sec.group_permission gp on gp.group_name = pg.group_name
                     where pg.person_id = p.person_id), '{}'),
           g.reason, g.granted_by, g.granted_at, g.expires_at,
           coalesce(g.expires_at <= now(), false)
    from sec.person p
    left join sec.access_grant g on g.person_id = p.person_id
    order by p.person_id, g.grant_id;
end
$$;

-- Policies -------------------------------------------------------------------------------

alter table core.fact_actuals enable row level security;
create policy read_in_scope on core.fact_actuals for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
    or (cost_center_id is null and (select sec.sees_all()))
);

alter table core.fact_gl_detail enable row level security;
create policy read_in_scope on core.fact_gl_detail for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
);

alter table core.fact_budget enable row level security;
create policy read_in_scope on core.fact_budget for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
);

alter table core.fact_budget_headcount enable row level security;
create policy read_in_scope on core.fact_budget_headcount for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
);

alter table core.employee enable row level security;
create policy read_in_scope on core.employee for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
);

-- Salary needs both the cost center and the comp_detail permission.
alter table core.employee_comp enable row level security;
create policy read_with_comp_permission on core.employee_comp for select using (
    (select sec.has_permission('comp_detail'))
    and cost_center_id in (select unnest(sec.visible_cost_centers()))
);

alter table core.requisition enable row level security;
create policy read_in_scope on core.requisition for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
);

alter table core.headcount_exception enable row level security;
create policy read_in_scope on core.headcount_exception for select using (
    cost_center_id in (select unnest(sec.visible_cost_centers()))
);

alter table core.forecast enable row level security;
create policy read_whole_units on core.forecast for select using (
    (unit_type || ':' || unit_id) in (select unnest(sec.visible_units()))
);

alter table core.forecast_backtest enable row level security;
create policy read_whole_units on core.forecast_backtest for select using (
    (unit_type || ':' || unit_id) in (select unnest(sec.visible_units()))
);

alter table core.variance_line enable row level security;
create policy read_whole_departments on core.variance_line for select using (
    ('department:' || department_id) in (select unnest(sec.visible_units()))
);
create policy review_whole_departments on core.variance_line for update
using (
    ('department:' || department_id) in (select unnest(sec.visible_units()))
    and (select sec.has_permission('review_variances'))
)
with check (
    ('department:' || department_id) in (select unnest(sec.visible_units()))
    and (select sec.has_permission('review_variances'))
    and reviewed_by = sec.current_person()
);

alter table ops.tieout_break enable row level security;
create policy read_if_all on ops.tieout_break for select using ((select sec.sees_all()));

-- Audit ----------------------------------------------------------------------------------

create or replace function ops.record(action text, object text, detail jsonb default '{}')
returns void
language sql volatile security definer set search_path = pg_catalog, ops, sec as $$
    insert into ops.audit_log (actor, action, object, detail)
    values (sec.current_person(), action, object, detail)
$$;

create or replace function ops.audit_grant_change() returns trigger
language plpgsql security definer set search_path = pg_catalog, ops, sec as $$
begin
    insert into ops.audit_log (actor, action, object, detail)
    values (coalesce(nullif(current_setting('app.actor', true), ''), session_user::text),
            'grant.' || lower(tg_op),
            coalesce(new.person_id, old.person_id),
            jsonb_build_object(
                'old', case when tg_op <> 'INSERT' then to_jsonb(old) end,
                'new', case when tg_op <> 'DELETE' then to_jsonb(new) end));
    return coalesce(new, old);
end
$$;

create trigger audit_grant_change
after insert or update or delete on sec.access_grant
for each row execute function ops.audit_grant_change();

create or replace function ops.audit_variance_review() returns trigger
language plpgsql security definer set search_path = pg_catalog, ops, sec as $$
begin
    if new.status is distinct from old.status or new.final_text is distinct from old.final_text then
        insert into ops.audit_log (actor, action, object, detail)
        values (coalesce(sec.current_person(), session_user::text),
                'variance.' || new.status,
                new.variance_id::text,
                jsonb_build_object('period', new.period, 'department_id', new.department_id,
                                   'account_number', new.account_number, 'draft', old.draft,
                                   'final_text', new.final_text, 'note', new.review_note));
    end if;
    return new;
end
$$;

create trigger audit_variance_review
after update on core.variance_line
for each row execute function ops.audit_variance_review();

-- Privileges for the API role --------------------------------------------------------------

grant usage on schema core, sec, ops to fpa_app;
grant select on all tables in schema core to fpa_app;
grant update (status, final_text, reviewed_by, reviewed_at, review_note) on core.variance_line to fpa_app;
grant select on sec.person to fpa_app;
grant select on ops.refresh_run, ops.tieout_break to fpa_app;
