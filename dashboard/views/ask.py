import streamlit as st

from common import ApiError, md, page_header, post

EXAMPLES = [
    "What did we spend on contractors in Engineering in September?",
    "Display advertising in Local News vs budget",
    "Net revenue year to date",
    "Digital subscriptions in Q3 vs last year",
]

page_header("Ask finance",
            "Questions are matched to an account or metric in the semantic layer and answered by a query that runs "
            "under your grants. The figures in an answer come from the database, never from a model.")


def ask(question: str) -> None:
    try:
        st.session_state["answer"] = (st.session_state["person"], question, post("/ask", {"question": question}))
    except ApiError as exc:
        st.session_state["answer"] = (st.session_state["person"], question, {"answer": exc.detail, "error": True})


if "question" not in st.session_state:
    st.session_state["question"] = st.query_params.get("q") or EXAMPLES[0]
    if st.query_params.get("q"):
        ask(st.query_params["q"])
st.markdown("**Try one**")
cols = st.columns(len(EXAMPLES))
for col, example in zip(cols, EXAMPLES, strict=True):
    if col.button(example, width="stretch"):
        st.session_state["question"] = example
        ask(example)

with st.form("ask"):
    question = st.text_input("Question", key="question")
    if st.form_submit_button("Ask", icon=":material/send:"):
        ask(question)

answer = st.session_state.get("answer")
if answer and answer[0] == st.session_state["person"] and answer[1] == st.session_state["question"]:
    result = answer[2]
    with st.container(border=True):
        if result.get("error"):
            st.error(md(result["answer"]))
        elif result.get("denied"):
            st.warning(md(result["answer"]), icon=":material/lock:")
        else:
            st.markdown(f"#### {md(result['answer'])}")
        if result.get("definition"):
            st.markdown(f"**Definition used**: {result['definition']}")
            for nuance in result.get("nuances") or []:
                st.markdown(f"- {nuance}")
        if result.get("query"):
            source = {"semantic_layer": "matched from the semantic layer's names and synonyms",
                      "claude": "translated by Claude, then checked against the semantic layer"}
            st.caption(f"Query {source.get(result.get('parsed_by'), '')}:")
            st.json(result["query"], expanded=False)
else:
    st.caption("Pick a question above or type your own.")
