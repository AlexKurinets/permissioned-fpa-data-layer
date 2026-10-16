import altair as alt
import pandas as pd
import streamlit as st

from common import SERIES, get, money, money_table, page_header, period_label

METHODS = {"plan_attainment": "Plan attainment (in use)", "remaining_budget": "Remaining budget (old method)",
           "holt_winters": "Holt-Winters (challenger)"}

period = st.session_state.get("period")
if not period:
    st.info("No period has been published yet.")
    st.stop()

page_header("Full-year outlook",
            f"Actuals through {period_label(period)} plus the rolling forecast for the rest of the year.")
data = get("/forecast", period=period)
revenue = pd.DataFrame(data["revenue"])
opex = pd.DataFrame(data["opex"])
if revenue.empty and opex.empty:
    st.warning("No forecast is visible under your grants. Forecasts are shown for whole departments and "
               "business units only.")
    st.stop()

if not revenue.empty:
    st.markdown("#### Revenue by business unit")
    st.caption("Each remaining budget month is scaled by how that revenue line has run against budget over its last "
               "three budgeted months, so the budget's seasonality and event calendar are kept.")
    unit = st.segmented_control("Business unit", list(revenue["unit_name"]), default=revenue["unit_name"].iloc[0]) \
        or revenue["unit_name"].iloc[0]
    row = revenue[revenue["unit_name"] == unit].iloc[0]
    a, b, c = st.columns(3)
    a.metric("Full-year outlook", money(row["outlook"]), border=True)
    b.metric("Full-year budget", money(row["fy_budget"]), border=True)
    c.metric("Outlook vs budget", money(row["outlook_vs_budget"]), border=True,
             help=f"The old method (actuals plus remaining budget) would say {money(row['old_method_outlook'])}.")

    series = pd.DataFrame(data["revenue_series"])
    series = series[series["unit_name"] == unit].copy()
    series["kind"] = series["kind"].map({"actual": "Actual", "budget": "Budget", "forecast": "Forecast"})
    # Start the forecast line at the last closed month so it reads as a continuation.
    last_actual = series[series["kind"] == "Actual"].sort_values("period").tail(1).assign(kind="Forecast")
    series = pd.concat([series, last_actual], ignore_index=True)
    series["period"] = pd.to_datetime(series["period"])
    base = alt.Chart(series).encode(
        x=alt.X("period:T", title=None, axis=alt.Axis(format="%b", tickCount=12)),
        y=alt.Y("amount:Q", title="Net revenue ($)", axis=alt.Axis(format="$,.2s")),
        color=alt.Color("kind:N", title=None, scale=alt.Scale(domain=["Actual", "Forecast", "Budget"], range=SERIES),
                        legend=alt.Legend(orient="top")),
        strokeDash=alt.StrokeDash("kind:N", scale=alt.Scale(domain=["Actual", "Forecast", "Budget"],
                                                            range=[[1, 0], [6, 3], [2, 3]]), legend=None),
        tooltip=[alt.Tooltip("kind:N", title=""), alt.Tooltip("period:T", title="Month", format="%B %Y"),
                 alt.Tooltip("amount:Q", title="Net revenue", format="$,.0f")],
    )
    chart = (base.mark_line(strokeWidth=2) + base.mark_point(size=60, filled=True)).properties(height=320)
    st.altair_chart(chart, width="stretch")

    st.markdown("#### How well each method would have done")
    st.caption("Backtest over the last six closed months: from each month, forecast one to three months ahead and "
               "compare with what closed. WAPE is total absolute error over total actual revenue; lower is better.")
    scores = pd.DataFrame(data["backtest"])
    scores = scores[scores["line"] == "net_revenue"].copy()
    names = dict(zip(revenue["unit_id"], revenue["unit_name"], strict=False))
    names["all"] = "All business units, pooled"
    scores["unit"] = scores["unit_id"].map(names)
    scores = scores.dropna(subset=["unit"])
    table = scores.pivot(index="unit", columns="method", values="wape")[list(METHODS)].rename(columns=METHODS) * 100
    table.index.name = "Business unit"
    st.dataframe(table, width="stretch",
                 column_config={m: st.column_config.NumberColumn(format="%.2f%%") for m in METHODS.values()})

if not opex.empty:
    st.markdown("#### Operating expenses by department")
    st.caption("Personnel cost per head over the last three months, times the heads expected once open roles fill "
               "(one month after the later of their plan and today), plus the non-personnel run-rate. "
               "Excludes depreciation and stock-based compensation.")
    table = opex[["department_name", "ytd_actual", "forecast_rest_of_year", "outlook", "fy_budget",
                  "outlook_vs_budget"]].rename(columns={
        "department_name": "Department", "ytd_actual": "YTD actual", "forecast_rest_of_year": "Rest of year",
        "outlook": "Outlook", "fy_budget": "FY budget", "outlook_vs_budget": "Outlook vs budget"})
    st.dataframe(money_table(table, ["YTD actual", "Rest of year", "Outlook", "FY budget", "Outlook vs budget"]),
                 hide_index=True, width="stretch", height=35 * (len(table) + 1) + 3)
