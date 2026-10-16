"""Drafters for variance commentary, and the validator every draft must pass.

The model is a writer here, not a calculator. It receives the facts the pipeline
computed and returns one or two sentences. Before a draft is stored, every number in
it is checked against the numbers in the facts. A draft with a number that is not in
the facts is rejected and replaced by the template draft, and the rejection is kept
on the line so the analyst can see it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from fpa.config import get_settings
from fpa.variance import template_draft

NUMBER = re.compile(
    r"(?P<dollar>\$)?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<suffix>%|percent\b|k\b|K\b|thousand\b|m\b|M\b|million\b)?"
)


@dataclass
class Draft:
    text: str
    source: str  # template | anthropic | template_after_rejection
    note: str | None = None


class Drafter(Protocol):
    name: str

    def draft(self, facts: dict) -> str: ...


def _fact_numbers(facts) -> tuple[set[float], set[float]]:
    """All numbers in the facts, split into percentages and everything else."""
    percents: set[float] = set()
    values: set[float] = set()

    def walk(obj, key: str = "") -> None:
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            (percents if key.endswith("pct") else values).add(float(obj))
        elif isinstance(obj, str):
            for m in NUMBER.finditer(obj):
                n = float(m["num"].replace(",", ""))
                (percents if m["suffix"] in ("%", "percent") else values).add(n)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, key)

    walk(facts)
    return percents, values


def unsupported_numbers(text: str, facts: dict) -> list[str]:
    """Numbers in `text` that do not match any number in `facts`."""
    percents, values = _fact_numbers(facts)
    magnitudes = {abs(v) for v in values}
    bad = []
    for m in NUMBER.finditer(text):
        raw = m.group(0).strip()
        n = float(m["num"].replace(",", ""))
        suffix = (m["suffix"] or "").lower()
        if suffix in ("%", "percent"):
            if not any(abs(abs(n) - abs(p)) <= 0.051 for p in percents):
                bad.append(raw)
            continue
        if suffix in ("k", "thousand"):
            n, tolerance = n * 1_000, 500
        elif suffix in ("m", "million"):
            n, tolerance = n * 1_000_000, 50_000
        else:
            tolerance = 50 if m["dollar"] else 0
        if not any(abs(n - v) <= tolerance for v in magnitudes):
            bad.append(raw)
    return bad


class TemplateDrafter:
    name = "template"

    def draft(self, facts: dict) -> str:
        return template_draft(facts)


SYSTEM_PROMPT = """You write first-pass budget-versus-actual commentary for a finance team.

You receive one budget line as JSON facts computed from the general ledger. Write one or
two plain sentences an FP&A analyst could paste into the monthly package.

Rules:
- Use only the facts given. Every number you write must appear in the facts, and dollar
  amounts and percentages must be written exactly as they appear in the display fields.
- Say what moved and the most likely driver from the facts (driver, vendors, headcount,
  prior_month, ytd, trend, same_month_last_year). If the facts do not explain the variance,
  say the driver is not yet identified rather than guessing.
- Do not speculate about causes the facts do not show, and do not recommend actions.
- No headings, no bullet points, no preamble."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"commentary": {"type": "string"}},
    "required": ["commentary"],
    "additionalProperties": False,
}


class DraftUnavailable(RuntimeError):
    pass


class AnthropicDrafter:
    """Claude drafts from the facts. Requires ANTHROPIC_API_KEY or an `ant auth login` profile."""

    name = "anthropic"

    def __init__(self, client=None, model: str | None = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model or get_settings().llm_model

    def draft(self, facts: dict) -> str:
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2048,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(facts, default=str)}],
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise DraftUnavailable(f"model unavailable: {type(exc).__name__}") from exc
        except anthropic.APIStatusError as exc:
            raise DraftUnavailable(f"model request failed with status {exc.status_code}") from exc
        if response.stop_reason == "refusal":
            raise DraftUnavailable("model declined the request")
        if response.stop_reason == "max_tokens":
            raise DraftUnavailable("model response was cut off")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)["commentary"].strip()


def get_drafter() -> Drafter:
    if get_settings().llm_provider == "anthropic":
        return AnthropicDrafter()
    return TemplateDrafter()


def draft_commentary(facts: dict, drafter: Drafter) -> Draft:
    if isinstance(drafter, TemplateDrafter):
        return Draft(template_draft(facts), "template")
    try:
        text = drafter.draft(facts)
    except DraftUnavailable as exc:
        return Draft(template_draft(facts), "template_after_rejection", str(exc))
    bad = unsupported_numbers(text, facts)
    if bad:
        return Draft(template_draft(facts), "template_after_rejection",
                     f"{drafter.name} draft rejected, numbers not in the facts: {', '.join(bad)}")
    if not text:
        return Draft(template_draft(facts), "template_after_rejection", f"{drafter.name} returned an empty draft")
    return Draft(text, drafter.name)
