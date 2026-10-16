"""Plain-language questions, answered from the semantic layer under the asker's grants.

A question becomes a structured query: one measure (an account or a metric from the
semantic layer), one period, an optional unit and a comparison. The query runs inside
the asker's scoped transaction, and the sentence is written by code from the result,
so the figures in an answer can only be figures the database returned for that person.

Two parsers produce the same Query. The offline one matches the catalog's names and
synonyms; the Claude one is used for phrasing the synonyms miss, and its output is
checked against the catalog before it is run.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date

import psycopg

from fpa.config import get_settings
from fpa.semantic import Catalog, is_favorable
from fpa.synthetic import add_months
from fpa.variance import money, month_label, pct

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})


class NotUnderstood(ValueError):
    pass


@dataclass
class Query:
    measure_kind: str  # account | metric
    measure_key: str
    span: str  # month | quarter | ytd
    period: str  # YYYY-MM, the last month of the span
    compare: str  # budget | prior_year
    unit_type: str | None = None  # business_unit | department | cost_center
    unit_id: str | None = None


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9&]+", " ", text.lower()).strip() + " "


def _units(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "select distinct business_unit_id, business_unit_name, department_id, department_name,"
        " cost_center_id, name from core.dim_cost_center").fetchall()
    units = set()
    for r in rows:
        units.add(("business_unit", r["business_unit_id"], r["business_unit_name"]))
        units.add(("department", r["department_id"], r["department_name"]))
        units.add(("cost_center", r["cost_center_id"], r["name"]))
    return sorted(units)


def _measures(catalog: Catalog) -> list[tuple[str, str, str]]:
    out = []
    for m in catalog.metrics.values():
        out += [("metric", m.key, w) for w in (m.label, *m.synonyms)]
    for a in catalog.accounts.values():
        if a.statement == "income_statement":
            out += [("account", a.number, w) for w in (a.name, *a.synonyms)]
    return out


def parse_offline(question: str, catalog: Catalog, units: list[tuple[str, str, str]], latest: date) -> Query:
    text = _norm(question)
    # Measures first, longest phrase wins, so "digital subscriptions" is not read as the
    # Subscriptions department.
    measure = None
    for kind, key, word in sorted(_measures(catalog), key=lambda m: -len(m[2])):
        needle = _norm(word)
        if needle in text:
            measure = (kind, key)
            text = text.replace(needle, " ")
            break
    unit = None
    for kind, uid, name in sorted(units, key=lambda u: -len(u[2])):
        needle = _norm(name)
        if needle in text:
            unit = (kind, uid)
            text = text.replace(needle, " ")
            break
    if measure is None:
        raise NotUnderstood("I couldn't match that to an account or metric in the semantic layer. "
                            "Try a name from the Definitions page, such as contractors or net revenue.")
    span, period = "month", latest
    year_match = re.search(r"\b(20\d\d)\b", text)
    year = int(year_match.group(1)) if year_match else latest.year
    q = re.search(r"\bq([1-4])\b", text)
    if " ytd " in text or "year to date" in text:
        span = "ytd"
    elif q:
        span, period = "quarter", date(year, int(q.group(1)) * 3, 1)
    else:
        for name, month in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])):
            if f" {name} " in text:
                period = date(year, month, 1)
                break
        if " last month " in text:
            period = latest
    if period > latest:
        period = date(period.year - 1, period.month, 1) if not year_match else latest
    compare = "prior_year" if re.search(r"last year|prior year|\byoy\b|year over year", text) else "budget"
    return Query(measure[0], measure[1], span, period.strftime("%Y-%m"), compare,
                 unit[0] if unit else None, unit[1] if unit else None)


QUERY_SCHEMA_PROMPT = """Translate a finance question into a query over the semantic layer.

Return measure_kind "metric" with a metric key, or "account" with a four-digit account
number, chosen only from the catalog provided. span is month, quarter or ytd; period is
YYYY-MM for the last month of the span and must not be later than the latest closed
period. compare is budget unless the question asks about last year. unit_type and
unit_id are null unless the question names a business unit, department or cost center
from the units list. If the question cannot be answered from the catalog, choose the
closest measure; the caller shows the definition it used."""


class ClaudeParser:
    def __init__(self, client=None, model: str | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model or get_settings().llm_model

    def parse(self, question: str, catalog: Catalog, units, latest: date) -> Query:
        catalog_text = json.dumps({
            "metrics": [{"key": m.key, "label": m.label, "synonyms": list(m.synonyms)}
                        for m in catalog.metrics.values()],
            "accounts": [{"number": a.number, "name": a.name, "synonyms": list(a.synonyms)}
                         for a in catalog.accounts.values() if a.statement == "income_statement"],
            "units": [{"type": t, "id": i, "name": n} for t, i, n in units],
            "latest_closed_period": latest.strftime("%Y-%m"),
        })
        schema = {
            "type": "object",
            "properties": {
                "measure_kind": {"type": "string", "enum": ["metric", "account"]},
                "measure_key": {"type": "string"},
                "span": {"type": "string", "enum": ["month", "quarter", "ytd"]},
                "period": {"type": "string"},
                "compare": {"type": "string", "enum": ["budget", "prior_year"]},
                "unit_type": {"anyOf": [{"type": "string", "enum": ["business_unit", "department", "cost_center"]},
                                        {"type": "null"}]},
                "unit_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["measure_kind", "measure_key", "span", "period", "compare", "unit_type", "unit_id"],
            "additionalProperties": False,
        }
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            system=[{"type": "text", "text": QUERY_SCHEMA_PROMPT}, {"type": "text", "text": catalog_text}],
            messages=[{"role": "user", "content": question}],
        )
        if response.stop_reason in ("refusal", "max_tokens"):
            raise NotUnderstood("The question could not be translated; try naming an account or metric.")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        query = Query(**data)
        if query.measure_kind == "metric" and query.measure_key not in catalog.metrics:
            raise NotUnderstood(f"Unknown metric {query.measure_key}.")
        if query.measure_kind == "account" and query.measure_key not in catalog.accounts:
            raise NotUnderstood(f"Unknown account {query.measure_key}.")
        if query.unit_id and (query.unit_type, query.unit_id) not in {(t, i) for t, i, _ in units}:
            raise NotUnderstood(f"Unknown unit {query.unit_id}.")
        return query


def _span(query: Query) -> tuple[date, date]:
    end = date.fromisoformat(query.period + "-01")
    if query.span == "ytd":
        return date(end.year, 1, 1), end
    if query.span == "quarter":
        return add_months(end, -2), end
    return end, end


def _span_label(query: Query) -> str:
    start, end = _span(query)
    if query.span == "ytd":
        return f"year to date through {month_label(end)}"
    if query.span == "quarter":
        return f"Q{(end.month - 1) // 3 + 1} {end.year}"
    return month_label(end)


def answer(conn: psycopg.Connection, catalog: Catalog, query: Query, me: dict, version: str) -> dict:
    if query.measure_kind == "metric":
        metric = catalog.metrics[query.measure_key]
        weights, label, definition, favorable_when = (catalog.metric_weights(metric.key), metric.label,
                                                      metric.definition, metric.favorable_when)
        nuances: list[str] = []
    else:
        account = catalog.accounts[query.measure_key]
        weights, label, definition, favorable_when = ({account.number: 1}, f"{account.name} ({account.number})",
                                                      account.definition, account.favorable_when)
        nuances = list(account.nuances)

    unit_filter, params = "", {}
    unit_name = "everything you have access to"
    if query.unit_id:
        column = {"business_unit": "business_unit_id", "department": "department_id",
                  "cost_center": "cost_center_id"}[query.unit_type]
        unit_name = conn.execute(
            f"select min(case when %(t)s = 'cost_center' then name when %(t)s = 'department' then department_name"
            f" else business_unit_name end) as n from core.dim_cost_center where {column} = %(id)s",
            {"t": query.unit_type, "id": query.unit_id}).fetchone()["n"]
        if query.unit_type != "cost_center" and f"{query.unit_type}:{query.unit_id}" not in me["units"]:
            seen = conn.execute(
                f"select count(*) filter (where cost_center_id = any(%(v)s)) as seen, count(*) as total"
                f" from core.dim_cost_center where {column} = %(id)s", {"v": me["cost_centers"], "id": query.unit_id},
            ).fetchone()
            reason = (f"You can see {seen['seen']} of the {seen['total']} cost centers in {unit_name}, "
                      "so a total for it isn't available to you." if seen["seen"]
                      else f"You don't have access to {unit_name}.")
            return {"answer": reason, "query": asdict(query), "definition": definition, "denied": True}
        if query.unit_type == "cost_center" and query.unit_id not in me["cost_centers"]:
            return {"answer": f"You don't have access to {unit_name}.", "query": asdict(query),
                    "definition": definition, "denied": True}
        unit_filter = f" and cc.{column} = %(unit)s"
        params["unit"] = query.unit_id

    start, end = _span(query)
    params.update({"nums": list(weights), "signs": list(weights.values()), "s": start, "e": end})
    actual = conn.execute(
        "select coalesce(sum(f.amount * w.sign), 0)::float as v from core.fact_actuals f"
        " join core.dim_cost_center cc using (cost_center_id)"
        " join unnest(%(nums)s::text[], %(signs)s::int[]) as w(account_number, sign) using (account_number)"
        f" where f.period between %(s)s and %(e)s{unit_filter}", params).fetchone()["v"]
    if query.compare == "budget":
        params["v"] = version
        other = conn.execute(
            "select coalesce(sum(f.amount * w.sign), 0)::float as v from core.fact_budget f"
            " join core.dim_cost_center cc using (cost_center_id)"
            " join unnest(%(nums)s::text[], %(signs)s::int[]) as w(account_number, sign) using (account_number)"
            f" where f.version = %(v)s and f.period between %(s)s and %(e)s{unit_filter}", params).fetchone()["v"]
        other_label = "budget"
    else:
        params.update({"s": date(start.year - 1, start.month, 1), "e": date(end.year - 1, end.month, 1)})
        other = conn.execute(
            "select coalesce(sum(f.amount * w.sign), 0)::float as v from core.fact_actuals f"
            " join core.dim_cost_center cc using (cost_center_id)"
            " join unnest(%(nums)s::text[], %(signs)s::int[]) as w(account_number, sign) using (account_number)"
            f" where f.period between %(s)s and %(e)s{unit_filter}", params).fetchone()["v"]
        other_label = "the same period last year"

    if abs(actual) < 0.005 and abs(other) < 0.005:
        text = f"{label} for {unit_name}, {_span_label(query)}: nothing recorded in the data you can see."
    else:
        variance = actual - other
        above = variance >= 0
        word = ("above" if above else "below") if favorable_when == "above_budget" else ("over" if above else "under")
        fav = "favorable" if is_favorable(favorable_when, variance) else "unfavorable"
        text = (f"{label} for {unit_name}, {_span_label(query)}: {money(actual)} actual against "
                f"{money(other)} {other_label}, {money(variance)} {word}")
        text += f" ({pct(variance / abs(other) * 100)}, {fav})." if other else f" ({fav})."
    return {"answer": text, "query": asdict(query), "definition": definition, "nuances": nuances,
            "actual": round(actual, 2), "comparison": round(other, 2), "denied": False}


def ask(conn: psycopg.Connection, catalog: Catalog, question: str, me: dict, latest: date, version: str,
        parser=None) -> dict:
    units = _units(conn)
    if parser is None and get_settings().llm_provider == "anthropic":
        parser = ClaudeParser()
    try:
        query = parse_offline(question, catalog, units, latest)
        parsed_by = "semantic_layer"
    except NotUnderstood:
        if parser is None:
            raise
        query = parser.parse(question, catalog, units, latest)
        parsed_by = "claude"
    result = answer(conn, catalog, query, me, version)
    result["parsed_by"] = parsed_by
    return result
