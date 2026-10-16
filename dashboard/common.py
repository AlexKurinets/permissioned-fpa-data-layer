"""Shared pieces for the dashboard pages: the API client, formatting and palette.

The dashboard holds no data of its own. Every number is fetched from the API with the
signed-in person's token, so it shows exactly what row-level security returns for them.
"""

from __future__ import annotations

import os

import requests
import streamlit as st

API_URL = os.environ.get("FPA_API_URL", "http://localhost:8000").rstrip("/")

# Reference palette: categorical slots 1-3 and the reserved status pair.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
MUTED = "#898781"


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _raise(response: requests.Response) -> None:
    if response.ok:
        return
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise ApiError(response.status_code, str(detail))


@st.cache_data(ttl=60, show_spinner=False)
def _cached_get(path: str, params: tuple, token: str):
    response = requests.get(f"{API_URL}{path}", params=dict(params),
                            headers={"Authorization": f"Bearer {token}"}, timeout=60)
    _raise(response)
    return response.json()


def get(path: str, **params):
    return _cached_get(path, tuple(sorted((k, v) for k, v in params.items() if v is not None)),
                       st.session_state["token"])


def post(path: str, body: dict):
    response = requests.post(f"{API_URL}{path}", json=body,
                             headers={"Authorization": f"Bearer {st.session_state['token']}"}, timeout=120)
    _raise(response)
    st.cache_data.clear()
    return response.json()


def download(path: str) -> bytes:
    response = requests.get(f"{API_URL}{path}", headers={"Authorization": f"Bearer {st.session_state['token']}"},
                            timeout=120)
    _raise(response)
    return response.content


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def signed(value: float) -> str:
    return ("+" if value >= 0 else "-") + f"${abs(value):,.0f}"


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


RUN_STATUS = {"published": "published", "failed_tieout": "failed the tie-out",
              "failed_coverage": "failed the coverage check", "failed": "failed", "running": "still running"}


def status_label(favorable: bool) -> str:
    return "▲ Favorable" if favorable else "▼ Unfavorable"


def period_label(period: str) -> str:
    from datetime import date

    year, month = period.split("-")[:2]
    return date(int(year), int(month), 1).strftime("%B %Y")


def md(text: str) -> str:
    """Escape dollar signs: Streamlit markdown reads a pair of them as a LaTeX formula."""
    return text.replace("$", "\\$")


def money_table(df, money_columns, pct_columns=()):
    """Whole-dollar display for finance tables; the underlying values stay numeric for sorting."""
    formats = {c: (lambda v: "" if v != v else money(v)) for c in money_columns}
    formats.update({c: (lambda v: "n/a" if v is None or v != v else f"{v:+.1f}%") for c in pct_columns})
    return df.style.format(formats)


def page_header(title: str, caption: str | None = None) -> None:
    st.title(title)
    me = st.session_state.get("me") or {}
    scope = ", ".join(me.get("scope_labels") or []) or "no data access"
    st.caption((caption + "  \n" if caption else "") +
               f"Signed in as {me.get('full_name', '?')} ({me.get('title', '')}). Scope: {scope}. "
               "Every figure is served under these grants.")
