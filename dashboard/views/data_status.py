import pandas as pd
import streamlit as st

from common import ApiError, get, money_table, page_header, period_label

page_header("Data status",
            "Nothing publishes unless the GL ties to the ERP trial balance and every account has a definition.")
status = get("/status")
latest, published = status["latest_run"], status["latest_published_run"]

if latest is None:
    st.info("No refresh has run yet.")
    st.stop()

ok = latest["status"] == "published"
with st.container(border=True):
    st.markdown(f"#### Latest refresh: {latest['run_id']}")
    if ok:
        st.success(f"Published {period_label(latest['period'])}. The GL tied to the ERP trial balance for every "
                   "entity, period and account, and every journal balanced.", icon=":material/verified:")
    else:
        st.error(f"{latest['status'].replace('_', ' ').capitalize()}. The published layer still shows "
                 f"{published['run_id'] if published else 'nothing'}.", icon=":material/error:")

if status["tieout_breaks"]:
    st.markdown("#### Tie-out breaks")
    breaks = pd.DataFrame(status["tieout_breaks"])
    breaks = breaks.rename(columns={"entity_id": "Entity", "period": "Period", "account_number": "Account",
                                    "check_name": "Check", "warehouse_amount": "Warehouse",
                                    "erp_amount": "ERP", "difference": "Difference"})
    st.dataframe(money_table(breaks, ["Warehouse", "ERP", "Difference"]), hide_index=True, width="stretch")
elif latest["status"] == "failed_tieout":
    st.caption("The break details are only visible to people with company-wide access.")

if published:
    s = published["summary"]
    v = s.get("variances", {})
    a, b, c, d = st.columns(4)
    a.metric("Accounts defined", s.get("accounts_defined"), border=True)
    b.metric("Material variance lines", v.get("material_lines"), border=True)
    c.metric("Drafts rejected by the validator", v.get("drafts_rejected"), border=True)
    d.metric("Payroll vs HRIS exceptions", s.get("headcount_exceptions"), border=True)
    st.caption(f"Last published: {published['run_id']}, finished {published['finished_at'][:19].replace('T', ' ')} "
               f"UTC. Drafts written by the {v.get('drafter', '?')} drafter.")

if "admin_access" in st.session_state["me"]["permissions"]:
    st.markdown("#### Access review")
    st.caption("Who can see what, for the quarterly sign-off. Every grant change is in the audit log.")
    try:
        review = pd.DataFrame(get("/admin/access-review"))
        review["Status"] = review.apply(
            lambda r: "No grant" if pd.isna(r["scope_type"]) else ("Expired" if r["expired"] else "Active"), axis=1)
        def scope(r) -> str:
            if pd.isna(r["scope_type"]):
                return ""
            if r["scope_type"] == "all":
                return "Whole company"
            return f"{r['scope_type'].replace('_', ' ')} {r['scope_value']}"

        review["Scope"] = review.apply(scope, axis=1)
        review["Permissions"] = review["permissions"].map(lambda p: ", ".join(x.replace("_", " ") for x in p))
        review["Expires"] = review["expires_at"].map(lambda e: e[:10] if isinstance(e, str) else "")
        columns = ["full_name", "Scope", "cost_center_count", "Permissions", "reason", "Expires", "Status"]
        table = review[columns].rename(columns={"full_name": "Person", "cost_center_count": "Cost centers",
                                                "reason": "Reason"})
        st.dataframe(table, hide_index=True, width="stretch", height=35 * (len(table) + 1) + 3)
    except ApiError as exc:
        st.error(exc.detail)
