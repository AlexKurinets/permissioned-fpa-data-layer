import copy

import yaml

from fpa.config import get_settings
from fpa.semantic import is_favorable, validate


def _raw():
    d = get_settings().semantic_dir
    return (yaml.safe_load((d / "accounts.yml").read_text(encoding="utf-8")),
            yaml.safe_load((d / "metrics.yml").read_text(encoding="utf-8")))


def test_catalog_is_valid_and_covers_every_account_in_the_erp_ledger_and_budget(catalog, owner):
    accounts, metrics = _raw()
    assert validate(accounts, metrics) == []
    in_sources = {r["account_number"] for r in owner.execute(
        "select account_number from src.erp_accounts union select account_number from src.erp_gl_lines"
        " union select account_number from src.plan_budget")}
    assert in_sources <= set(catalog.accounts)
    assert len(catalog.accounts) == 63


def test_validator_catches_short_definitions_shared_synonyms_and_unknown_sections():
    accounts, metrics = _raw()
    broken = copy.deepcopy(accounts)
    broken["accounts"][0]["definition"] = "Ad revenue."
    broken["accounts"][1]["synonyms"].append("direct ad sales")  # already used by 4010
    broken["accounts"][2]["section"] = "Revenues"
    problems = validate(broken, metrics)
    assert any("4010: definition is shorter" in p for p in problems)
    assert any("'direct ad sales' is used by both account 4010 and account 4020" in p for p in problems)
    assert any("unknown section Revenues" in p for p in problems)


def test_reporting_sign_comes_from_the_section_so_contra_revenue_reads_negative(catalog, owner):
    assert catalog.accounts["4010"].reporting_sign == -1
    assert catalog.accounts["4900"].reporting_sign == -1
    assert catalog.accounts["6010"].reporting_sign == 1
    row = owner.execute(
        "select sum(amount) filter (where account_number = '4010') as display,"
        " sum(amount) filter (where account_number = '4900') as commissions"
        " from core.fact_actuals where period = '2026-09-01'").fetchone()
    assert row["display"] > 0
    assert row["commissions"] < 0


def test_metrics_exclude_non_cash_and_intercompany_accounts(catalog):
    ebitda = catalog.metric_weights("adjusted_ebitda")
    assert ebitda["4010"] == 1 and ebitda["5010"] == -1 and ebitda["6010"] == -1
    for excluded in ("7140", "6070", "8100", "8110", "8900"):
        assert excluded not in ebitda
    personnel = catalog.metric_weights("personnel_cost")
    assert "6050" in personnel and "6070" not in personnel
    assert catalog.metric_weights("net_revenue")["4900"] == 1


def test_favorable_direction():
    assert is_favorable("above_budget", 10) and not is_favorable("above_budget", -10)
    assert is_favorable("below_budget", -10) and not is_favorable("below_budget", 10)
