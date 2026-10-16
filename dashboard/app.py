"""Streamlit entry point: sign in, pick a period, navigate.

In dev mode the sidebar stands in for the identity provider's login: choosing a person
asks the API for a token signed as that person. In production the dashboard sits behind
the company SSO and forwards the signed-in user's token instead.
"""

from __future__ import annotations

import requests
import streamlit as st

from common import API_URL, ApiError, get, period_label

st.set_page_config(page_title="FP&A data layer", page_icon=":material/account_balance:", layout="wide")

DEFAULT_PERSON = "maya.ortiz@example.com"


@st.cache_data(ttl=300, show_spinner=False)
def people() -> list[dict]:
    response = requests.get(f"{API_URL}/auth/dev-people", timeout=30)
    response.raise_for_status()
    return response.json()


def sign_in(person_id: str) -> None:
    if st.session_state.get("person") == person_id and st.session_state.get("token"):
        return
    response = requests.post(f"{API_URL}/auth/dev-token", json={"person_id": person_id}, timeout=30)
    response.raise_for_status()
    st.session_state["person"] = person_id
    st.session_state["token"] = response.json()["access_token"]


directory = people()
ids = [p["person_id"] for p in directory]
wanted = st.query_params.get("as") or st.session_state.get("person") or DEFAULT_PERSON
if wanted not in ids:
    wanted = DEFAULT_PERSON

with st.sidebar:
    st.subheader("FP&A data layer")
    choice = st.selectbox(
        "Signed in as",
        ids,
        index=ids.index(wanted),
        format_func=lambda pid: next(f"{p['full_name']}, {p['title']}" for p in directory if p["person_id"] == pid),
        help="Dev mode: stands in for the SSO login. Each person gets a token signed for them.",
    )
sign_in(choice)
st.query_params["as"] = choice

try:
    me = get("/me")
    status = get("/status")
except ApiError as exc:
    st.error(f"The API refused the request: {exc.detail}")
    st.stop()
st.session_state["me"] = me
periods = status["periods"]

with st.sidebar:
    if periods:
        st.session_state["period"] = st.selectbox("Period", periods, format_func=period_label)
    st.markdown("**Data access**")
    for label in me["scope_labels"] or ["No data access granted"]:
        st.markdown(f"- {label}")
    if me["permissions"]:
        st.markdown("**Permissions**: " + ", ".join(p.replace("_", " ") for p in me["permissions"]))
    latest = status["latest_run"]
    if latest and latest["status"] != "published":
        st.error(f"Latest refresh {latest['status'].replace('_', ' ')}. Showing the last published data.",
                 icon=":material/error:")

pages = [
    st.Page("views/bva.py", title="Budget vs actual", icon=":material/table_chart:", default=True),
    st.Page("views/variances.py", title="Variance review", icon=":material/rate_review:", url_path="variances"),
    st.Page("views/headcount.py", title="Headcount", icon=":material/groups:", url_path="headcount"),
    st.Page("views/forecast.py", title="Forecast", icon=":material/trending_up:", url_path="forecast"),
    st.Page("views/ask.py", title="Ask finance", icon=":material/chat:", url_path="ask"),
    st.Page("views/definitions.py", title="Definitions", icon=":material/menu_book:", url_path="definitions"),
    st.Page("views/data_status.py", title="Data status", icon=":material/fact_check:", url_path="data-status"),
]
st.navigation(pages).run()
