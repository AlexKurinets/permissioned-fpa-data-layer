"""The semantic layer: account definitions and metrics, loaded from semantic/*.yml.

One catalog drives the reporting sign, which direction is favorable, how metrics roll
up, what the dashboard glossary shows and what the question answerer is allowed to
mean by a word. If the catalog does not validate, nothing publishes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fpa.config import get_settings

MIN_DEFINITION_CHARS = 40
STATEMENTS = {"income_statement", "balance_sheet"}
NATURES = {"debit", "credit"}
DIRECTIONS = {"above_budget", "below_budget"}


class CatalogError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("semantic layer is invalid:\n  " + "\n  ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Section:
    name: str
    statement: str
    nature: str
    favorable_when: str | None

    @property
    def reporting_sign(self) -> int:
        # Debits are positive in the ledger. Credit-natured sections flip, so revenue
        # reads positive and a contra-revenue account inside it reads negative.
        return -1 if self.nature == "credit" else 1


@dataclass(frozen=True)
class Account:
    number: str
    name: str
    section: Section
    group: str
    owner: str
    definition: str
    nuances: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    tags: frozenset[str] = frozenset()
    budgeted: bool = True

    @property
    def statement(self) -> str:
        return self.section.statement

    @property
    def reporting_sign(self) -> int:
        return self.section.reporting_sign

    @property
    def favorable_when(self) -> str | None:
        return self.section.favorable_when


@dataclass(frozen=True)
class Term:
    sign: int
    section: str | None = None
    group: str | None = None
    exclude_tags: frozenset[str] = frozenset()

    def matches(self, account: Account) -> bool:
        if self.section and account.section.name != self.section:
            return False
        if self.group and account.group != self.group:
            return False
        return not (account.tags & self.exclude_tags)


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    favorable_when: str
    definition: str
    synonyms: tuple[str, ...]
    terms: tuple[Term, ...]


@dataclass(frozen=True)
class Catalog:
    version: int
    sections: dict[str, Section]
    accounts: dict[str, Account]
    metrics: dict[str, Metric]
    min_abs_variance: float
    min_pct_variance: float
    _weights: dict[str, dict[str, int]] = field(default_factory=dict, repr=False)

    def metric_weights(self, key: str) -> dict[str, int]:
        """Account number -> +1/-1 contribution of that account to the metric.

        Intercompany accounts never contribute: they are eliminated in consolidation.
        """
        if key not in self._weights:
            metric = self.metrics[key]
            weights: dict[str, int] = {}
            for account in self.accounts.values():
                if "eliminate_on_consolidation" in account.tags:
                    continue
                for term in metric.terms:
                    if term.matches(account):
                        weights[account.number] = weights.get(account.number, 0) + term.sign
            self._weights[key] = {n: s for n, s in weights.items() if s}
        return self._weights[key]

    def budgeted_accounts(self) -> list[Account]:
        return [a for a in self.accounts.values() if a.statement == "income_statement" and a.budgeted]


def is_favorable(favorable_when: str | None, variance: float) -> bool:
    if favorable_when == "below_budget":
        return variance <= 0
    return variance >= 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def validate(raw_accounts: dict, raw_metrics: dict) -> list[str]:
    problems: list[str] = []
    sections = raw_accounts.get("sections") or {}
    for name, s in sections.items():
        if s.get("statement") not in STATEMENTS:
            problems.append(f"section {name}: statement must be one of {sorted(STATEMENTS)}")
        if s.get("nature") not in NATURES:
            problems.append(f"section {name}: nature must be debit or credit")
        if s.get("statement") == "income_statement" and s.get("favorable_when") not in DIRECTIONS:
            problems.append(f"section {name}: income statement sections need favorable_when")

    seen_numbers: set[str] = set()
    words: dict[str, str] = {}  # normalized synonym or name -> owner of that word

    def claim(word: str, owner: str) -> None:
        key = _norm(word)
        if key in words and words[key] != owner:
            problems.append(f"'{word}' is used by both {words[key]} and {owner}")
        words[key] = owner

    groups: set[str] = set()
    for a in raw_accounts.get("accounts") or []:
        number = str(a.get("number", ""))
        label = f"account {number or '?'}"
        if not re.fullmatch(r"\d{4}", number):
            problems.append(f"{label}: number must be four digits")
        if number in seen_numbers:
            problems.append(f"{label}: duplicate number")
        seen_numbers.add(number)
        for required in ("name", "section", "group", "owner", "definition"):
            if not a.get(required):
                problems.append(f"{label}: missing {required}")
        if a.get("section") and a["section"] not in sections:
            problems.append(f"{label}: unknown section {a['section']}")
        if len((a.get("definition") or "").strip()) < MIN_DEFINITION_CHARS:
            problems.append(f"{label}: definition is shorter than {MIN_DEFINITION_CHARS} characters")
        groups.add(a.get("group") or "")
        claim(a.get("name") or number, label)
        for synonym in a.get("synonyms") or []:
            claim(synonym, label)

    for m in raw_metrics.get("metrics") or []:
        label = f"metric {m.get('key', '?')}"
        if m.get("favorable_when") not in DIRECTIONS:
            problems.append(f"{label}: favorable_when must be above_budget or below_budget")
        if len((m.get("definition") or "").strip()) < MIN_DEFINITION_CHARS // 2:
            problems.append(f"{label}: definition is too short")
        claim(m.get("label") or m.get("key", ""), label)
        for synonym in m.get("synonyms") or []:
            claim(synonym, label)
        for t in m.get("terms") or []:
            if t.get("section") and t["section"] not in sections:
                problems.append(f"{label}: unknown section {t['section']}")
            if t.get("group") and t["group"] not in groups:
                problems.append(f"{label}: unknown group {t['group']}")
            if t.get("sign") not in (1, -1):
                problems.append(f"{label}: term sign must be 1 or -1")
    return problems


def load_catalog(directory: Path | None = None) -> Catalog:
    directory = directory or get_settings().semantic_dir
    raw_accounts = yaml.safe_load((directory / "accounts.yml").read_text(encoding="utf-8"))
    raw_metrics = yaml.safe_load((directory / "metrics.yml").read_text(encoding="utf-8"))
    problems = validate(raw_accounts, raw_metrics)
    if problems:
        raise CatalogError(problems)

    sections = {
        name: Section(name, s["statement"], s["nature"], s.get("favorable_when"))
        for name, s in raw_accounts["sections"].items()
    }
    accounts: dict[str, Account] = {}
    for a in raw_accounts["accounts"]:
        section = sections[a["section"]]
        accounts[str(a["number"])] = Account(
            number=str(a["number"]),
            name=a["name"],
            section=section,
            group=a["group"],
            owner=a["owner"],
            definition=" ".join(a["definition"].split()),
            nuances=tuple(a.get("nuances") or ()),
            synonyms=tuple(a.get("synonyms") or ()),
            tags=frozenset(a.get("tags") or ()),
            budgeted=a.get("budgeted", section.statement == "income_statement"),
        )
    metrics = {
        m["key"]: Metric(
            key=m["key"],
            label=m["label"],
            favorable_when=m["favorable_when"],
            definition=" ".join(m["definition"].split()),
            synonyms=tuple(m.get("synonyms") or ()),
            terms=tuple(
                Term(
                    sign=t["sign"],
                    section=t.get("section"),
                    group=t.get("group"),
                    exclude_tags=frozenset(t.get("exclude_tags") or ()),
                )
                for t in m["terms"]
            ),
        )
        for m in raw_metrics["metrics"]
    }
    materiality = raw_metrics.get("materiality") or {}
    return Catalog(
        version=int(raw_accounts.get("version", 1)),
        sections=sections,
        accounts=accounts,
        metrics=metrics,
        min_abs_variance=float(materiality.get("min_abs_variance", 25000)),
        min_pct_variance=float(materiality.get("min_pct_variance", 0.05)),
    )


def coverage_gaps(catalog: Catalog, account_numbers: Iterable[str]) -> list[str]:
    """Accounts that exist in the ERP or carry activity but have no definition."""
    return sorted({n for n in account_numbers if n not in catalog.accounts})
