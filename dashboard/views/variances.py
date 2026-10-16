import streamlit as st

from common import ApiError, get, md, money, page_header, period_label, post, signed, status_label

DRIVERS = {
    "timing": "Timing", "new_vendor": "New vendor", "vendor": "Vendor", "headcount": "Headcount",
    "volume": "Volume", "other": "Not yet identified",
}
STATUS = {"draft": ("Draft", "orange"), "accepted": ("Accepted", "green"), "edited": ("Edited", "green"),
          "rejected": ("Rejected", "red")}

period = st.session_state.get("period")
if not period:
    st.info("No period has been published yet.")
    st.stop()

page_header(f"Variance review, {period_label(period)}",
            "Each material line has a first-pass explanation drafted from facts the pipeline computed. Every number "
            "in a draft was checked against those facts before it was stored.")
lines = get("/variances", period=period)["lines"]
can_review = "review_variances" in st.session_state["me"]["permissions"]
if not lines:
    st.info("No material variances in the departments your grants cover in full. Cost-center-only access shows "
            "line-level BvA on the Budget vs actual page but not department commentary.")
    st.stop()

reviewed = sum(1 for v in lines if v["status"] != "draft")
st.progress(reviewed / len(lines), text=f"{reviewed} of {len(lines)} lines reviewed")

for v in lines:
    with st.container(border=True):
        head, badges = st.columns([3, 2])
        head.markdown(f"#### {v['department_name']} · {v['account_name']} ({v['account_number']})")
        label, color = STATUS[v["status"]]
        badges.markdown(f":{color}-badge[{label}] :gray-badge[Driver: {DRIVERS[v['driver']]}] "
                        f":gray-badge[{'Claude' if v['draft_source'] == 'anthropic' else 'Template'} draft]")
        a, b, c, d = st.columns(4)
        a.metric("Actual", money(v["actual"]))
        b.metric("Budget", money(v["budget"]))
        c.metric("Variance", signed(v["variance"]), f"{v['variance_pct']:+.1f}%" if v["variance_pct"] else None,
                 delta_color="off")
        d.metric("Status", status_label(v["favorable"]))
        text = v["final_text"] if v["status"] in ("accepted", "edited") else v["draft"]
        st.markdown(f"> {md(text)}")
        if v["validator_note"]:
            st.warning(md(v["validator_note"]), icon=":material/policy:")
        if v["reviewed_by"]:
            st.caption(f"{label} by {v['reviewed_by']} on {v['reviewed_at'][:10]}."
                       + (f" Note: {md(v['review_note'])}" if v["review_note"] else ""))
        elif v["review_note"]:
            st.caption(v["review_note"])
        with st.expander("Facts behind this draft"):
            facts = v["facts"]
            st.markdown(f"**Definition used**: {facts['account_definition']}")
            for nuance in facts.get("account_nuances") or []:
                st.markdown(f"- {nuance}")
            st.json({k: facts[k] for k in ("ytd", "prior_month", "same_month_last_year", "trend", "vendors",
                                           "headcount") if k in facts}, expanded=False)
        if can_review and v["status"] == "draft":
            key = f"v{v['variance_id']}"
            try:
                x, z, _ = st.columns([1, 1, 4])
                if x.button("Accept draft", key=f"{key}-a", icon=":material/check:"):
                    post(f"/variances/{v['variance_id']}/review", {"action": "accept"})
                    st.rerun()
                if z.button("Reject", key=f"{key}-r", icon=":material/close:"):
                    post(f"/variances/{v['variance_id']}/review", {"action": "reject"})
                    st.rerun()
                with st.expander("Edit before accepting"):
                    edited = st.text_area("Commentary", value=v["draft"], key=f"{key}-text", height=110)
                    if st.button("Save edit", key=f"{key}-e", icon=":material/edit:"):
                        post(f"/variances/{v['variance_id']}/review", {"action": "edit", "text": edited})
                        st.rerun()
            except ApiError as exc:
                st.error(exc.detail)
        elif not can_review:
            st.caption("Reviewing needs the review_variances permission, which FP&A holds.")
