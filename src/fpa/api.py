"""HTTP API. Every data endpoint runs inside a transaction scoped to the caller."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from fpa import ask as ask_module
from fpa import headcount, package, reporting
from fpa.auth import AuthError, DevIssuer, Identity, Verifier
from fpa.config import get_settings
from fpa.db import app_pool, scoped
from fpa.semantic import load_catalog


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.catalog = load_catalog()
    app.state.issuer = DevIssuer(settings) if settings.auth_mode == "dev" else None
    app.state.verifier = Verifier(settings, app.state.issuer)
    app.state.pool = app_pool()
    try:
        yield
    finally:
        app.state.pool.close()


app = FastAPI(title="Permissioned FP&A data layer", version="1.0.0", lifespan=lifespan)


def identity(request: Request, authorization: Annotated[str | None, Header()] = None) -> Identity:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        return request.app.state.verifier.verify(authorization.split(" ", 1)[1].strip())
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


class Caller:
    def __init__(self, request: Request, ident: Identity):
        self.request = request
        self.person_id = ident.person_id

    def tx(self):
        return scoped(self.request.app.state.pool, self.person_id)


def caller(request: Request, ident: Annotated[Identity, Depends(identity)]) -> Caller:
    c = Caller(request, ident)
    with c.tx() as conn:
        if reporting.whoami(conn) is None:
            raise HTTPException(403, f"{ident.person_id} is not an active person in the data layer")
    return c


CallerDep = Annotated[Caller, Depends(caller)]


def _period(request: Request, conn: psycopg.Connection, period: str | None) -> date:
    if period:
        try:
            return reporting.parse_period(period)
        except ValueError as exc:
            raise HTTPException(422, "period must look like 2026-09") from exc
    published = reporting.periods(conn)
    if not published:
        raise HTTPException(404, "no published period yet")
    return published[0]


@app.get("/health")
def health(request: Request) -> dict:
    with request.app.state.pool.connection() as conn:
        run = conn.execute(
            "select run_id, period, status from ops.refresh_run order by started_at desc limit 1").fetchone()
    return {"status": "ok", "auth_mode": request.app.state.settings.auth_mode, "latest_run": run}


@app.get("/.well-known/jwks.json")
def jwks(request: Request) -> dict:
    if request.app.state.issuer is None:
        raise HTTPException(404, "the API does not issue tokens outside dev mode")
    return request.app.state.issuer.jwks()


@app.get("/auth/dev-people")
def dev_people(request: Request) -> list[dict]:
    """Dev mode only: the people a demo can sign in as. Stands in for the IdP login page."""
    if request.app.state.issuer is None:
        raise HTTPException(404, "not available outside dev mode")
    with request.app.state.pool.connection() as conn:
        return conn.execute(
            "select person_id, full_name, title from sec.person where active order by full_name").fetchall()


class DevTokenRequest(BaseModel):
    person_id: str


@app.post("/auth/dev-token")
def dev_token(request: Request, body: DevTokenRequest) -> dict:
    if request.app.state.issuer is None:
        raise HTTPException(404, "not available outside dev mode")
    token = request.app.state.issuer.issue(body.person_id.lower())
    return {"access_token": token, "token_type": "bearer", "expires_in": 8 * 3600}


@app.get("/me")
def me(c: CallerDep) -> dict:
    with c.tx() as conn:
        who = reporting.whoami(conn)
        names = {}
        for r in conn.execute("select * from core.dim_cost_center"):
            names[("business_unit", r["business_unit_id"])] = r["business_unit_name"]
            names[("department", r["department_id"])] = r["department_name"]
            names[("cost_center", r["cost_center_id"])] = f"{r['name']} ({r['cost_center_id']})"
    labels = []
    for g in who["grants"]:
        if g["scope_type"] == "all":
            labels.append("Whole company")
        else:
            kind = g["scope_type"].replace("_", " ").capitalize()
            labels.append(f"{kind}: {names.get((g['scope_type'], g['scope_value']), g['scope_value'])}")
    return {**who, "scope_labels": labels}


@app.get("/status")
def status(c: CallerDep) -> dict:
    with c.tx() as conn:
        out = reporting.status(conn)
        out["periods"] = [p.strftime("%Y-%m") for p in reporting.periods(conn)]
        return out


@app.get("/semantic/accounts")
def semantic_accounts(request: Request, c: CallerDep) -> list[dict]:
    catalog = request.app.state.catalog
    return [{"number": a.number, "name": a.name, "section": a.section.name, "group": a.group,
             "statement": a.statement, "definition": a.definition, "nuances": list(a.nuances),
             "synonyms": list(a.synonyms), "owner": a.owner, "tags": sorted(a.tags),
             "favorable_when": a.favorable_when, "normal_side": a.section.nature}
            for a in catalog.accounts.values()]


@app.get("/semantic/metrics")
def semantic_metrics(request: Request, c: CallerDep) -> list[dict]:
    catalog = request.app.state.catalog
    return [{"key": m.key, "label": m.label, "definition": m.definition, "synonyms": list(m.synonyms),
             "favorable_when": m.favorable_when, "accounts": catalog.metric_weights(m.key)}
            for m in catalog.metrics.values()]


@app.get("/bva")
def bva(request: Request, c: CallerDep, period: str | None = None) -> dict:
    settings, catalog = request.app.state.settings, request.app.state.catalog
    with c.tx() as conn:
        p = _period(request, conn, period)
        who = reporting.whoami(conn)
        return {
            "period": p.strftime("%Y-%m"),
            "summary": reporting.bva_summary(conn, catalog, p, settings.budget_version),
            "lines": reporting.bva_lines(conn, catalog, p, settings.budget_version, who["units"]),
            "materiality": {"min_abs_variance": catalog.min_abs_variance,
                            "min_pct_variance": catalog.min_pct_variance},
        }


@app.get("/variances")
def variances(request: Request, c: CallerDep, period: str | None = None) -> dict:
    with c.tx() as conn:
        p = _period(request, conn, period)
        return {"period": p.strftime("%Y-%m"), "lines": reporting.variance_list(conn, p)}


class Review(BaseModel):
    action: Literal["accept", "edit", "reject"]
    text: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=500)


@app.post("/variances/{variance_id}/review")
def review(c: CallerDep, variance_id: int, body: Review) -> dict:
    if body.action == "edit" and not (body.text or "").strip():
        raise HTTPException(422, "an edit needs the replacement text")
    with c.tx() as conn:
        row = reporting.review_variance(conn, variance_id, body.action, c.person_id, body.text, body.note)
    if row is None:
        raise HTTPException(403, "you cannot review this line: it is outside your scope or you lack review rights")
    return row


@app.get("/headcount")
def headcount_view(request: Request, c: CallerDep, period: str | None = None) -> dict:
    with c.tx() as conn:
        return headcount.report(conn, _period(request, conn, period), request.app.state.settings.budget_version)


@app.get("/forecast")
def forecast_view(request: Request, c: CallerDep, period: str | None = None) -> dict:
    settings, catalog = request.app.state.settings, request.app.state.catalog
    with c.tx() as conn:
        return reporting.forecast_view(conn, catalog, _period(request, conn, period), settings.budget_version)


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@app.post("/ask")
def ask(request: Request, c: CallerDep, body: Question) -> dict:
    settings, catalog = request.app.state.settings, request.app.state.catalog
    with c.tx() as conn:
        latest = _period(request, conn, None)
        who = reporting.whoami(conn)
        try:
            result = ask_module.ask(conn, catalog, body.question, who, latest, settings.budget_version)
        except ask_module.NotUnderstood as exc:
            result = {"answer": str(exc), "query": None, "denied": False, "parsed_by": None}
        conn.execute("select ops.record('ask', %s, %s)",
                     (body.question[:200], Jsonb({"query": result.get("query"), "denied": result.get("denied")})))
        return result


@app.get("/packages/{period}.pptx")
def package_download(request: Request, c: CallerDep, period: str) -> Response:
    settings, catalog = request.app.state.settings, request.app.state.catalog
    with c.tx() as conn:
        p = _period(request, conn, period)
        who = reporting.whoami(conn)
        try:
            content = package.build(conn, catalog, p, settings.budget_version, who)
        except package.PackageBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        conn.execute("select ops.record('package.download', %s, %s)", (p.strftime("%Y-%m"), Jsonb({})))
    filename = f"bva-package-{p:%Y-%m}-{c.person_id.split('@')[0]}.pptx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/admin/access-review")
def access_review(c: CallerDep) -> list[dict]:
    with c.tx() as conn:
        try:
            return conn.execute("select * from sec.access_review()").fetchall()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise HTTPException(403, "the access review requires the admin_access permission") from exc


def main() -> None:
    import os

    import uvicorn

    uvicorn.run("fpa.api:app", host="0.0.0.0", port=int(os.environ.get("API_PORT", "8000")))
