from conftest import CFO, PERIOD
from fpa import pipeline


def _fingerprint(conn):
    return conn.execute(
        "select (select sum(amount) from core.fact_actuals) as actuals,"
        " (select count(*) from core.variance_line) as lines,"
        " (select max(run_id) from ops.refresh_run where status = 'published') as run").fetchone()


def test_published_run_tied_out_and_recorded_its_summary(owner, published):
    run = owner.execute("select * from ops.refresh_run where run_id = %s", (published["run_id"],)).fetchone()
    assert run["status"] == "published"
    assert run["summary"]["accounts_defined"] == 63
    assert pipeline.check_tieout(owner) == []


def test_a_missing_gl_line_blocks_publishing_and_keeps_the_last_good_month(owner, api, token):
    before = _fingerprint(owner)
    dropped = pipeline.drop_line(owner, PERIOD)
    try:
        result = pipeline.refresh(owner, PERIOD)
        assert result["status"] == "failed_tieout"
        breaks = owner.execute("select * from ops.tieout_break where run_id = %s", (result["run_id"],)).fetchall()
        by_check = {b["check_name"]: b for b in breaks}
        assert by_check["activity_by_account"]["account_number"] == dropped["account_number"]
        assert float(by_check["activity_by_account"]["difference"]) == -dropped["amount"]
        assert "journals_balance" in by_check
        assert _fingerprint(owner) == before  # nothing published

        response = api.get("/packages/2026-09.pptx", headers=token(CFO))
        assert response.status_code == 409
        assert "failed_tieout" in response.json()["detail"]
        status = api.get("/status", headers=token(CFO)).json()
        assert status["latest_run"]["status"] == "failed_tieout"
        assert status["tieout_breaks"]
    finally:
        assert pipeline.restore_lines(owner) == 1
        assert pipeline.refresh(owner, PERIOD)["status"] == "published"


def test_an_account_without_a_definition_blocks_publishing(owner):
    owner.execute("insert into src.erp_accounts values ('6099', 'Temporary staffing', 'Operating expenses')")
    try:
        result = pipeline.refresh(owner, PERIOD)
        assert result["status"] == "failed_coverage"
        assert result["unmapped_accounts"] == ["6099"]
    finally:
        owner.execute("delete from src.erp_accounts where account_number = '6099'")
        assert pipeline.refresh(owner, PERIOD)["status"] == "published"


def test_republishing_the_same_numbers_keeps_drafts_and_reviews(owner):
    result = pipeline.refresh(owner, PERIOD)
    assert result["variances"]["drafted"] == 0
    assert result["variances"]["kept_from_last_run"] == result["variances"]["material_lines"] == 10


def test_redraft_rewrites_only_unreviewed_lines(owner):
    target = owner.execute("select variance_id from core.variance_line where account_number = '7030'").fetchone()
    owner.execute("update core.variance_line set status = 'accepted', final_text = draft, reviewed_by = %s,"
                  " reviewed_at = now() where variance_id = %s", (CFO, target["variance_id"]))
    try:
        result = pipeline.refresh(owner, PERIOD, redraft=True)
        assert result["variances"]["drafted"] == 9 and result["variances"]["kept_from_last_run"] == 1
    finally:
        owner.execute("update core.variance_line set status = 'draft', final_text = null, reviewed_by = null,"
                      " reviewed_at = null where variance_id = %s", (target["variance_id"],))
