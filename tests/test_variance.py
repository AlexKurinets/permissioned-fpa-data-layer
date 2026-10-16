import json
from types import SimpleNamespace

import pytest

from conftest import ANALYST, PERIOD, VP_ENG
from fpa import pipeline, reporting
from fpa.drafting import AnthropicDrafter, draft_commentary, unsupported_numbers
from fpa.variance import template_draft

EXPECTED = {
    ("CB-SAL", "4010"): "volume", ("CB-SAL", "4030"): "volume", ("LN-SAL", "4010"): "volume",
    ("LN-SAL", "4020"): "volume", ("LE-OPS", "4210"): "volume", ("LE-OPS", "5110"): "vendor",
    ("LE-OPS", "5120"): "vendor", ("CORP-ENG", "6010"): "headcount", ("CORP-ENG", "6050"): "new_vendor",
    ("CORP-MKT", "7030"): "timing",
}


@pytest.fixture
def lines(owner):
    rows = owner.execute("select * from core.variance_line where period = %s", (PERIOD,)).fetchall()
    return {(r["department_id"], r["account_number"]): r for r in rows}


def test_material_lines_and_their_drivers(lines):
    assert {k: v["driver"] for k, v in lines.items()} == EXPECTED


def test_every_material_line_clears_both_thresholds(lines, catalog):
    for line in lines.values():
        assert abs(line["variance"]) >= catalog.min_abs_variance
        assert abs(line["variance_pct"]) >= catalog.min_pct_variance * 100


def test_template_drafts_only_use_numbers_from_their_facts(lines):
    for line in lines.values():
        assert line["draft_source"] == "template"
        assert unsupported_numbers(line["draft"], line["facts"]) == [], line["draft"]


def test_drafts_tell_the_planted_stories(lines):
    contractors = lines[("CORP-ENG", "6050")]["draft"]
    assert "Kettlebrook Contract Engineering, first billed in July 2026" in contractors
    assert "4 Platform engineering roles" in contractors
    assert "points to timing" in lines[("CORP-MKT", "7030")]["draft"]
    assert "every month since June 2026" in lines[("CB-SAL", "4010")]["draft"]


class FakeDrafter:
    name = "fake"

    def __init__(self, text):
        self.text = text

    def draft(self, facts):
        return self.text


def test_a_draft_with_an_invented_number_is_replaced_by_the_template(lines):
    facts = lines[("CORP-ENG", "6050")]["facts"]
    invented = FakeDrafter("Contractors ran $52,000 over budget because three engineers left in August.")
    draft = draft_commentary(facts, invented)
    assert draft.source == "template_after_rejection"
    assert "$52,000" in draft.note
    assert draft.text == template_draft(facts)


def test_a_draft_using_only_fact_numbers_is_kept(lines):
    facts = lines[("CORP-ENG", "6050")]["facts"]
    honest = FakeDrafter("Contractors were $44,100 over budget (122.5%), almost all of it $44,000 from "
                         "Kettlebrook Contract Engineering, which started billing in July 2026.")
    draft = draft_commentary(facts, honest)
    assert draft.source == "fake" and draft.note is None


class FakeMessages:
    def __init__(self, stop_reason="end_turn", commentary="Contractors were $44,100 over budget."):
        self.calls = []
        self.stop_reason = stop_reason
        self.commentary = commentary

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps({"commentary": self.commentary})
        return SimpleNamespace(stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)])


def _client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def test_claude_drafter_sends_facts_with_structured_output_and_fallbacks(lines):
    facts = lines[("CORP-ENG", "6050")]["facts"]
    messages = FakeMessages()
    draft = draft_commentary(facts, AnthropicDrafter(client=_client(messages), model="claude-opus-5-5"))
    assert draft.source == "anthropic" and draft.text == "Contractors were $44,100 over budget."
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert json.loads(call["messages"][0]["content"])["account_number"] == "6050"


def test_a_refusal_falls_back_to_the_template(lines):
    facts = lines[("CORP-ENG", "6050")]["facts"]
    draft = draft_commentary(facts, AnthropicDrafter(client=_client(FakeMessages("refusal")), model="m"))
    assert draft.source == "template_after_rejection" and "declined" in draft.note


def test_review_is_scoped_audited_and_survives_a_rerun(as_person, owner):
    with as_person(ANALYST) as conn:
        line = next(v for v in reporting.variance_list(conn, PERIOD) if v["account_number"] == "7030")
    with as_person(VP_ENG) as conn:
        assert reporting.review_variance(conn, line["variance_id"], "accept", VP_ENG, None, None) is None
    with as_person(ANALYST) as conn:
        row = reporting.review_variance(conn, line["variance_id"], "edit", ANALYST,
                                        "Timing: the fall brand event invoice landed in August.", "checked AP")
    assert row["status"] == "edited"
    audit = owner.execute("select actor, action from ops.audit_log where object = %s order by audit_id desc limit 1",
                          (str(line["variance_id"]),)).fetchone()
    assert audit == {"actor": ANALYST, "action": "variance.edited"}
    pipeline.refresh(owner, PERIOD)
    kept = owner.execute("select status, final_text from core.variance_line where variance_id = %s",
                         (line["variance_id"],)).fetchone()
    assert kept["status"] == "edited"
    owner.execute("update core.variance_line set status = 'draft', final_text = null, reviewed_by = null,"
                  " reviewed_at = null, review_note = null where variance_id = %s", (line["variance_id"],))
