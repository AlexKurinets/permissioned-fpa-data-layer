import altair as alt
import pandas as pd
import streamlit as st

from common import SERIES, get, money_table, page_header, period_label

KINDS = {"active_not_paid": "Active, not paid", "paid_not_active": "Paid, not active",
         "cost_center_mismatch": "Cost center differs", "paid_not_in_hris": "Paid, no HRIS record"}

period = st.session_state.get("period")
if not period:
    st.info("No period has been published yet.")
    st.stop()

page_header(f"Headcount, {period_label(period)}",
            "Plan is the planning workbook's FTE plan for the month: budgeted positions, including new roles "
            "from their planned start, less a 2% vacancy allowance.")
data = get("/headcount", period=period)
by_cc = pd.DataFrame(data["by_cost_center"])
if by_cc.empty:
    st.warning("No headcount is visible under your grants.")
    st.stop()

dept = by_cc.groupby("department_name", as_index=False)[["plan", "actual", "open_roles"]].sum()
a, b, c, d = st.columns(4)
a.metric("Actual heads", f"{dept['actual'].sum():,}", border=True)
b.metric("Plan (FTE)", f"{dept['plan'].sum():,.0f}", border=True)
c.metric("Gap to plan", f"{dept['actual'].sum() - dept['plan'].sum():+,.0f}", border=True)
d.metric("Open budgeted roles", f"{dept['open_roles'].sum():,}", border=True)

long = dept.melt(id_vars="department_name", value_vars=["plan", "actual"], var_name="series", value_name="heads")
long["series"] = long["series"].map({"plan": "Plan", "actual": "Actual"})
chart = (
    alt.Chart(long)
    .mark_bar(cornerRadiusEnd=4)
    .encode(
        y=alt.Y("department_name:N", title=None, sort="-x", axis=alt.Axis(labelLimit=220),
                scale=alt.Scale(paddingInner=0.3)),
        yOffset=alt.YOffset("series:N", sort=["Plan", "Actual"]),
        x=alt.X("heads:Q", title="Heads at month end"),
        color=alt.Color("series:N", title=None, scale=alt.Scale(domain=["Plan", "Actual"], range=SERIES[:2]),
                        legend=alt.Legend(orient="top")),
        tooltip=[alt.Tooltip("department_name:N", title="Department"), alt.Tooltip("series:N", title=""),
                 alt.Tooltip("heads:Q", title="Heads")],
    )
    .properties(height=alt.Step(13), title="Plan vs actual by department")
)
st.altair_chart(chart, width="stretch")

with st.container():
    st.markdown("#### Open budgeted roles")
    roles = pd.DataFrame(data["open_roles"])
    if roles.empty:
        st.caption("None past their planned start.")
    else:
        roles["planned_start"] = pd.to_datetime(roles["planned_start"]).dt.strftime("%b %Y")
        st.dataframe(roles[["req_id", "title", "level", "cost_center", "planned_start"]].rename(columns={
            "req_id": "Req", "title": "Title", "level": "Level", "cost_center": "Cost center",
            "planned_start": "Planned start"}), hide_index=True, width="stretch")
with st.container():
    st.markdown("#### Payroll vs HRIS exceptions")
    ex = pd.DataFrame(data["exceptions"])
    if ex.empty:
        st.success("Payroll and HRIS agree for every visible employee.", icon=":material/check_circle:")
    else:
        ex["kind"] = ex["kind"].map(KINDS)
        st.dataframe(ex[["kind", "full_name", "cost_center", "detail"]].rename(columns={
            "kind": "Exception", "full_name": "Employee", "cost_center": "Cost center", "detail": "Detail"}),
            hide_index=True, width="stretch",
            column_config={"Detail": st.column_config.TextColumn(width="large")})

st.markdown("#### People")
people = pd.DataFrame(data["employees"])
columns = {"full_name": "Name", "title": "Title", "level": "Level", "cost_center": "Cost center",
           "start_date": "Start date"}
if data["salary_visible"]:
    columns["annual_salary"] = "Annual salary"
else:
    st.caption("Salaries are hidden: they need the comp_detail permission as well as access to the cost center.")
people = people[list(columns)].rename(columns=columns)
st.dataframe(money_table(people, ["Annual salary"] if data["salary_visible"] else []), hide_index=True,
             width="stretch", height=320)
