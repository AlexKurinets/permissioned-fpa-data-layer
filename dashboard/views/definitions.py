import pandas as pd
import streamlit as st

from common import get, page_header

page_header("Definitions",
            "The semantic layer: one plain-language definition per account, maintained by finance in "
            "semantic/accounts.yml. The BvA signs, the variance drafts and the question answerer all read from it.")
accounts = pd.DataFrame(get("/semantic/accounts"))
metrics = get("/semantic/metrics")

search = st.text_input("Search accounts", placeholder="contractors, deferred revenue, 6050 ...")
sections = st.multiselect("Sections", list(dict.fromkeys(accounts["section"])))
view = accounts
if search:
    needle = search.lower()
    view = view[view.apply(lambda r: needle in " ".join([r["number"], r["name"], r["definition"],
                                                         " ".join(r["synonyms"]), " ".join(r["nuances"])]).lower(),
                           axis=1)]
if sections:
    view = view[view["section"].isin(sections)]

st.caption(f"{len(view)} of {len(accounts)} accounts")
for _, a in view.iterrows():
    with st.expander(f"{a['number']} · {a['name']}  ({a['section']} / {a['group']})"):
        st.markdown(a["definition"])
        for nuance in a["nuances"]:
            st.markdown(f"- {nuance}")
        details = [f"Owner: {a['owner']}", f"Normal side: {a['normal_side']}"]
        if isinstance(a["favorable_when"], str):
            details.append("Favorable when " + a["favorable_when"].replace("_", " "))
        if a["synonyms"]:
            details.append("Also called: " + ", ".join(a["synonyms"]))
        if a["tags"]:
            details.append("Tags: " + ", ".join(a["tags"]))
        st.caption(" · ".join(details))

st.markdown("#### Metrics")
for m in metrics:
    with st.expander(m["label"]):
        st.markdown(m["definition"])
        plus = [n for n, s in m["accounts"].items() if s > 0]
        minus = [n for n, s in m["accounts"].items() if s < 0]
        st.caption(f"Adds {len(plus)} accounts" + (f", subtracts {len(minus)}" if minus else "")
                   + ". Favorable when " + m["favorable_when"].replace("_", " ") + ".")
