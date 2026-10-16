# Permissioned FP&A data layer

A finance data layer that gives every budget owner their own budget-vs-actual, headcount and forecast, and shows them nothing else. Actuals come from the ERP general ledger, the budget and FTE plan from the planning workbook, and headcount from the HRIS and payroll. A refresh publishes all of it only after the ledger ties to the ERP trial balance and every account has a plain-language definition. Postgres row-level security then decides, person by person, which rows exist at all. On top of that layer sit a first-pass BvA with drafted variance commentary, a headcount reconciliation, a rolling forecast, a PowerPoint package built per person, a plain-language question box and a Streamlit dashboard.

This is a reference implementation of the architecture behind a platform I built for a mid-sized digital media company's finance team. The production build is under NDA, so a few integrations are abstracted behind adapters: the ERP, planning, HRIS and payroll extracts, and the identity provider. It runs on synthetic data, and every company, place, person, phone number and figure in it is made up.

[![ci](https://github.com/AlexKurinets/permissioned-fpa-data-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexKurinets/permissioned-fpa-data-layer/actions/workflows/ci.yml)

## Architecture

```mermaid
flowchart LR
  subgraph Sources["Source extracts (adapters)"]
    ERP["ERP: GL detail, trial balance"]
    PLAN["Planning: budget, FTE plan"]
    HRIS["HRIS: roster, requisitions"]
    PAY["Payroll register"]
  end
  SEM[("semantic/*.yml")]
  subgraph Refresh["fpa refresh, owner role"]
    COV["Coverage gate"]
    TIE["Tie-out gate"]
    PUB["Publish in one transaction"]
    VAR["Variance facts, drafts, validator"]
    FC["Forecast and backtest"]
  end
  subgraph PG["Postgres 16"]
    CORE[("core: facts, variance lines")]
    SEC[("sec: people, grants, RLS")]
    OPS[("ops: runs, breaks, audit")]
  end
  IDP["Identity provider, OIDC"]
  API["FastAPI as fpa_app"]
  DASH["Streamlit dashboard"]
  LLM["Claude, optional"]
  Sources --> COV --> TIE --> PUB
  SEM --> COV
  PUB --> CORE
  PUB --> VAR --> CORE
  PUB --> FC --> CORE
  SEM --> VAR
  VAR -. "facts only" .-> LLM
  IDP -. "token" .-> API
  CORE --> API
  SEC --> API
  API --> DASH
```

The synthetic company has two legal entities, four business units, 13 departments and 17 cost centers. It has 36 months of double-entry GL (October 2023 to September 2026, about 19,900 lines), an FY26 budget and FTE plan, 458 employees at the end of September 2026, 26 budgeted new roles and 12 people with logins.

## Design decisions

**Permissions live in Postgres, not in the API.** Every fact table has a row-level security policy ([sql/002_security.sql](sql/002_security.sql)). The API connects as `fpa_app`, a role that owns nothing. At the start of each transaction it sets `app.person_id` from the verified token with `set_config(..., true)` ([src/fpa/db.py](src/fpa/db.py)). Because the setting is transaction-local, a pooled connection cannot carry one person's scope into the next request. Grants are per person, resolve down the hierarchy (business unit, then department, then cost center), carry a reason and an optional expiry, and are audited by a trigger. A person with no live grant sees nothing. The dashboard, the package builder and the question answerer all read through the same policies, so none of them can show more than the database allows.

**Totals only where you can see every part.** A department, business unit or company total is visible only to someone who can see all of its cost centers (`sec.visible_units()`). Otherwise a manager of one cost center could subtract their own numbers from the department total and recover a colleague's. In practice the Platform engineering manager gets cost-center-level BvA, while department-level commentary and forecasts stay with the VP of Engineering.

**Salary is its own table with its own permission.** `core.employee_comp` needs both the cost center and the `comp_detail` permission, which comes from identity provider groups. Headcount exception details never include pay amounts, because budget owners can see exceptions but not individual pay.

**The semantic layer is a reviewed file, and the code depends on it.** [semantic/accounts.yml](semantic/accounts.yml) defines all 63 accounts in plain language, with nuances, synonyms, an owner and tags. The section an account sits in decides its reporting sign and which direction is favorable. That is why agency commissions, a debit-balance contra account, read as negative revenue. [semantic/metrics.yml](semantic/metrics.yml) defines Net revenue, Adjusted EBITDA and the rest once, along with the materiality thresholds ($25,000 and 5%). [src/fpa/semantic.py](src/fpa/semantic.py) rejects short definitions, synonyms shared between accounts and unknown sections. The refresh refuses to publish if the ERP chart, the GL or the budget contains an account the file does not define.

**Nothing publishes unless the ledger ties.** [src/fpa/pipeline.py](src/fpa/pipeline.py) checks that the GL detail the adapters landed reproduces the ERP's own trial balance activity for every entity, period and account, and that every entity's journals balance. If a check fails, the run is recorded as `failed_tieout` with its breaks, the published layer keeps the last good month, the dashboard shows a banner and the package endpoint answers 409. If both gates pass, dimensions, facts, exceptions, variance lines and the forecast are replaced in a single transaction.

**The model writes sentences, not numbers.** [src/fpa/variance.py](src/fpa/variance.py) finds the material lines and computes their facts: year to date, the prior month, the same month last year, the trend, top vendors with first-billed month, and open budgeted roles. It then classifies the driver as timing, new vendor, vendor, headcount or volume. [src/fpa/drafting.py](src/fpa/drafting.py) turns the facts into one or two sentences, using either a deterministic template or Claude through the Anthropic SDK (structured output, server-side refusal fallback). Every number in a draft is checked against the facts before the draft is stored. A draft with an invented number is replaced by the template draft, and the rejection stays visible on the line. FP&A accepts, edits or rejects each line. Reviews are audited, survive reruns with unchanged numbers, and reset to draft if the numbers move.

**Questions become queries.** [src/fpa/ask.py](src/fpa/ask.py) maps a question to one account or metric, a period and an optional unit, using the semantic layer's names and synonyms. Claude is used only for phrasing the synonyms miss, and its output is checked against the catalog. The query runs under the asker's grants and code writes the sentence, so an answer can only contain figures the database returned for that person. Asking about a unit you can only partly see gets a refusal rather than a partial total.

**A forecast finance can audit.** [src/fpa/forecast.py](src/fpa/forecast.py) re-forecasts each revenue line by scaling the remaining budget months by that line's actual-to-budget ratio over its last three budgeted months, so the budget's own seasonality and event calendar are kept. On a six-origin, one-to-three-month backtest pooled across business units, the weighted absolute error is 1.46%. The old assumption that the remaining months land on budget scores 2.29%. A Holt-Winters challenger scores 2.20% in the Compose stack and does worst on the lumpy Live Events calendar (4.28%). Its parameters come from a numerical optimizer, so its score moves slightly by platform; the other two scores are plain arithmetic and do not. Operating expenses are forecast from the FTE plan, as cost per head times the heads expected once open roles fill.

**Headcount reconciles to payroll.** [src/fpa/headcount.py](src/fpa/headcount.py) compares the HRIS roster with the payroll register each period and flags people active but not paid, paid but not active, and paid to a different cost center. The September 2026 data has one of each.

**The package is built per person.** [src/fpa/package.py](src/fpa/package.py) builds the monthly PowerPoint from the same scoped queries, so the VP of Engineering and the CFO request the same package and get different decks. Unreviewed commentary is marked Draft.

## Running it

```bash
cp .env.example .env              # optional: change host ports or turn on Claude drafting
docker compose up --build -d --wait
```

The `pipeline` service migrates, seeds the synthetic company if the database is empty, refreshes and exits. It is the only container with the owner login; the API only ever has `fpa_app`. Then:

- Dashboard: http://localhost:8501. Use the sidebar to sign in as different people. In dev mode it stands in for the SSO login and gets a token signed for that person.
- API docs: http://localhost:8000/docs

Good people to compare: Maya Ortiz (CFO, whole company), Dana Whitfield (VP Engineering, one department, no salaries), Omar Haddad (Platform engineering manager, one cost center), Leo Marsh (GM Local News, one business unit), Sam Keller (FP&A lead, can review drafts, see salaries and run the access review), Hana Kowalski (external auditor whose grant expired) and Ben Foster (no grant yet).

### Break the tie-out, watch publishing stop, put it back

```bash
docker compose run --rm pipeline fpa simulate drop-line --period 2026-09   # lose one GL line on the way in
docker compose run --rm pipeline fpa refresh                               # exits 2: failed_tieout, nothing published
docker compose run --rm pipeline fpa simulate restore
docker compose run --rm pipeline fpa refresh                               # published again
```

While the refresh is failed, the dashboard shows a banner and the Data status page lists the breaks. The package endpoint returns 409.

### Other operator commands

```bash
docker compose run --rm pipeline fpa access review --as sam.keller@example.com
docker compose run --rm pipeline fpa ask "net revenue year to date" --as dana.whitfield@example.com
docker compose run --rm pipeline fpa package --period 2026-09 --as dana.whitfield@example.com --out /tmp/engineering.pptx
docker compose run --rm pipeline fpa access grant grace.lin@example.com department CORP-ENG   --reason "interim Engineering budget owner" --by sam.keller@example.com --expires 2030-12-31   # prints the grant id
docker compose run --rm pipeline fpa access revoke <grant id> --by sam.keller@example.com
```

Grant changes take effect on the next request and are written to `ops.audit_log` by a trigger.

### Claude drafting

Set `FPA_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in `.env`, then run `docker compose run --rm pipeline fpa refresh --redraft`. This rewrites unreviewed drafts and leaves reviewed ones alone. The model defaults to `claude-opus-5-5` (`FPA_LLM_MODEL`). Whatever the model writes goes through the same number check as the template.

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export FPA_OWNER_DSN=postgresql://fpa:fpa@localhost:5432/fpa FPA_APP_DSN=postgresql://fpa_app:fpa_app@localhost:5432/fpa
fpa migrate && fpa seed && fpa refresh
uvicorn fpa.api:app --port 8000 &
FPA_API_URL=http://localhost:8000 streamlit run dashboard/app.py
```

## Tests

```bash
pytest          # needs Postgres at FPA_OWNER_DSN; the suite rebuilds the schema
ruff check .
```

The suite runs against a real Postgres, because row-level security cannot be faked in memory. It covers default deny, every grant shape, the expired grant, the subtraction rule, salary permission, pooled connections not leaking identity, and the API role failing to disable RLS or read grants. It also covers both publishing gates, the material lines and their drivers, the number validator against an invented figure, the Claude drafter against a fake client (request shape, refusal fallback), the review workflow and its audit, the backtest, the headcount exceptions, the API's token checks, the per-person package, and every dashboard page rendering for three different scopes. CI runs the suite, then brings the Compose stack up and runs [scripts/smoke.sh](scripts/smoke.sh), which executes every command block in this README.

## Layout

```
semantic/          accounts.yml (63 accounts), metrics.yml (metrics, materiality)
sql/               001_schema.sql, 002_security.sql (policies, grant functions, audit triggers)
src/fpa/           pipeline, semantic, variance, drafting, forecast, headcount, ask, package, api, cli
src/fpa/synthetic.py   the made-up company and its source extracts
dashboard/         Streamlit app, one file per page under views/
scripts/smoke.sh   every README sequence against a running stack
tests/             pytest against Postgres, plus a Streamlit AppTest smoke test
```

## What is deliberately left out

- **A real identity provider.** In dev mode the API signs RS256 tokens itself and verifies them with the same code that checks a provider's JWKS (`FPA_AUTH_MODE=oidc`). Group membership is seeded rather than synced over SCIM.
- **Live connectors to the ERP, planning tool, HRIS and payroll.** The adapters' job ends at the `src` schema, and here the synthetic generator fills it.
- **Direct BI-tool connections.** The database trusts whoever connects as `fpa_app` to set `app.person_id` honestly. A BI tool would need per-user database roles or a gateway that sets it.
- **Multi-currency.** Both entities report in USD, and there is no FX translation.
- **Scheduling.** `fpa refresh` is a command; production ran it on the close calendar.
- **Live model calls in CI.** The Claude paths are tested against a fake client. Running them for real needs an API key and costs money.

## License

MIT, Alex Kurinets.
