import altair as alt
import pandas as pd
import streamlit as st

from common import (
    CRITICAL,
    GOOD,
    ApiError,
    download,
    get,
    money,
    money_table,
    page_header,
    pct,
    period_label,
    signed,
    status_label,
)

period = st.session_state.get("period")
if not period:
    st.info("No period has been published yet.")
    st.stop()

page_header(f"Budget vs actual, {period_label(period)}",
            "First-pass BvA from the published layer. Lines that clear both materiality thresholds are flagged "
            "and carry a draft explanation on the Variance review page.")
data = get("/bva", period=period)
summary = [m for m in data["summary"] if m["in_scope"]]
lines = pd.DataFrame(data["lines"])
if not summary or lines.empty:
    st.warning("Nothing to show: no budgeted lines are visible under your grants.")
    st.stop()

cols = st.columns(min(4, len(summary)))
order = ["net_revenue", "gross_profit", "operating_expenses", "adjusted_ebitda", "personnel_cost", "cost_of_revenue"]
for col, m in zip(cols, sorted(summary, key=lambda m: order.index(m["key"]))[:4], strict=False):
    month = m["month"]
    col.metric(
        m["label"],
        money(month["actual"]),
        f"{signed(month['variance'])} vs budget ({pct(month['variance_pct'])})",
        delta_color="normal" if m["favorable_when"] == "above_budget" else "inverse",
        border=True,
        help=f"Budget {money(month['budget'])}. Year to date: {money(m['ytd']['actual'])} actual, "
             f"{signed(m['ytd']['variance'])} vs budget.",
    )

units = sorted(lines["unit_name"].unique())
material = lines[lines["material"]]
# Open on the unit with the largest material variance, so the first screen has something to explain.
first = material.loc[material["variance"].abs().idxmax(), "unit_name"] if not material.empty else units[0]
unit = st.segmented_control("Unit", units, default=first, selection_mode="single") or first
view = lines[lines["unit_name"] == unit].copy()
view["status"] = view["favorable"].map(status_label)
st.markdown(f"#### {unit}: every budgeted line")

top = view.reindex(view["variance"].abs().sort_values(ascending=False).index).head(8)
chart = (
    alt.Chart(top)
    .mark_bar(cornerRadiusEnd=4)
    .encode(
        y=alt.Y("account_name:N", sort=None, title=None, axis=alt.Axis(labelLimit=220),
                scale=alt.Scale(paddingInner=0.35)),
        x=alt.X("variance:Q", title="Variance vs budget ($)", axis=alt.Axis(format="$,.0f", grid=True)),
        color=alt.Color("status:N", title=None,
                        scale=alt.Scale(domain=["▲ Favorable", "▼ Unfavorable"], range=[GOOD, CRITICAL]),
                        legend=alt.Legend(orient="top")),
        tooltip=[alt.Tooltip("account_name:N", title="Line"), alt.Tooltip("actual:Q", title="Actual", format="$,.0f"),
                 alt.Tooltip("budget:Q", title="Budget", format="$,.0f"),
                 alt.Tooltip("variance:Q", title="Variance", format="+$,.0f"),
                 alt.Tooltip("variance_pct:Q", title="Var %", format="+.1f"), alt.Tooltip("status:N", title="")],
    )
    .properties(height=alt.Step(30), title="Largest variances this month")
)
st.altair_chart(chart, width="stretch")

table = view[["account_number", "account_name", "section", "actual", "budget", "variance", "variance_pct",
              "status", "material"]].rename(columns={
    "account_number": "Account", "account_name": "Line", "section": "Section", "actual": "Actual",
    "budget": "Budget", "variance": "Variance", "variance_pct": "Var %", "status": "Status", "material": "Material"})
st.dataframe(
    money_table(table, ["Actual", "Budget", "Variance"], ["Var %"]),
    hide_index=True,
    width="stretch",
    height=35 * (len(table) + 1) + 3,
    column_config={"Material": st.column_config.CheckboxColumn(
        help="Clears both thresholds: $25,000 and 5% of budget")},
)

thresholds = data["materiality"]
st.caption(f"Material means at least \\{money(thresholds['min_abs_variance'])} and "
           f"{thresholds['min_pct_variance']:.0%} away from budget. Signs follow the semantic layer: revenue above "
           "budget and costs under budget are favorable.")

st.divider()
left, right = st.columns([3, 2])
left.markdown("**Monthly package**  \nThe same numbers as a PowerPoint deck, built for your scope, with draft "
              "commentary marked until FP&A reviews it.")
if right.button("Build my package", icon=":material/slideshow:", width="stretch"):
    try:
        content = download(f"/packages/{period}.pptx")
        right.download_button("Download .pptx", content, file_name=f"bva-package-{period}.pptx",
                              mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                              width="stretch")
    except ApiError as exc:
        right.error(exc.detail)
