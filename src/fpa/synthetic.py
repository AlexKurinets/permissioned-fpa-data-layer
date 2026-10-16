"""Synthetic source systems for a made-up digital media company.

Everything here is invented: the company, its entities, cost centers, employees,
vendors and every amount. The generator writes the same shapes the real adapters
land (ERP GL detail and trial balance, the planning workbook, HRIS and payroll), so
the pipeline cannot tell the difference.

The ledger is double-entry: every P&L line has a balance-sheet offset in the same
journal, so each entity's activity sums to zero every month and the tie-out is a
real check. A handful of stories are planted in 2026 so the September 2026 close has
variances worth explaining:

  * Engineering's Platform team has four budgeted roles (July to September) still
    open, and a contractor firm started billing in July to cover the work.
  * Marketing's fall brand event was budgeted in September but invoiced in August.
  * Brands display advertising has run ahead of budget since June.
  * The September live event sold fewer tickets than planned and overran on venue costs.
  * Three payroll-vs-HRIS exceptions in September (a late final paycheck, a new
    starter missing from payroll and a transfer not yet synced).
"""

from __future__ import annotations

import calendar
import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

FIRST_PERIOD = date(2023, 10, 1)
LAST_PERIOD = date(2026, 9, 1)
BUDGET_YEAR = 2026
BUDGET_VERSION = "FY26 Budget"
SEED = 20260930
VACANCY_FACTOR = 0.98  # the budget assumes 2% of seats are empty at any time


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def all_periods() -> list[date]:
    out, p = [], FIRST_PERIOD
    while p <= LAST_PERIOD:
        out.append(p)
        p = add_months(p, 1)
    return out


HORIZON_END = month_end(LAST_PERIOD)

ENTITIES = [
    ("EMG-US", "Example Media Group, Inc.", "USD"),
    ("EMG-EV", "Example Media Events LLC", "USD"),
]


@dataclass(frozen=True)
class CostCenter:
    id: str
    name: str
    dept_id: str
    dept_name: str
    bu_id: str
    bu_name: str
    entity: str
    seats: int
    family: str


COST_CENTERS = [
    CostCenter("1110", "Lifestyle editorial", "CB-EDT", "Brands Editorial", "CB", "Consumer Brands", "EMG-US", 47, "editorial"),
    CostCenter("1120", "Food editorial", "CB-EDT", "Brands Editorial", "CB", "Consumer Brands", "EMG-US", 32, "editorial"),
    CostCenter("1210", "Brands ad sales", "CB-SAL", "Brands Ad Sales", "CB", "Consumer Brands", "EMG-US", 37, "sales"),
    CostCenter("1310", "Audience growth", "CB-AUD", "Audience Growth", "CB", "Consumer Brands", "EMG-US", 21, "audience"),
    CostCenter("2110", "Metro desk", "LN-NWS", "Newsroom", "LN", "Local News", "EMG-US", 74, "editorial"),
    CostCenter("2120", "Sports desk", "LN-NWS", "Newsroom", "LN", "Local News", "EMG-US", 32, "editorial"),
    CostCenter("2210", "Local ad sales", "LN-SAL", "Local Ad Sales", "LN", "Local News", "EMG-US", 32, "sales"),
    CostCenter("2310", "Subscriptions", "LN-SUB", "Subscriptions", "LN", "Local News", "EMG-US", 16, "subscriptions"),
    CostCenter("3110", "Event operations", "LE-OPS", "Event Operations", "LE", "Live Events", "EMG-EV", 26, "events"),
    CostCenter("3210", "Sponsorship sales", "LE-SPN", "Sponsorship Sales", "LE", "Live Events", "EMG-EV", 16, "sales"),
    CostCenter("4110", "Platform engineering", "CORP-ENG", "Engineering", "CORP", "Corporate", "EMG-US", 50, "engineering"),
    CostCenter("4120", "Data engineering", "CORP-ENG", "Engineering", "CORP", "Corporate", "EMG-US", 19, "data"),
    CostCenter("4130", "Product design", "CORP-ENG", "Engineering", "CORP", "Corporate", "EMG-US", 16, "design"),
    CostCenter("4210", "Marketing", "CORP-MKT", "Marketing", "CORP", "Corporate", "EMG-US", 23, "marketing"),
    CostCenter("4310", "Finance", "CORP-FIN", "Finance", "CORP", "Corporate", "EMG-US", 13, "finance"),
    CostCenter("4410", "People", "CORP-PPL", "People", "CORP", "Corporate", "EMG-US", 11, "people"),
    CostCenter("4510", "General and administrative", "CORP-GA", "General and Administrative", "CORP", "Corporate", "EMG-US", 9, "ga"),
]
CC = {c.id: c for c in COST_CENTERS}

LEVEL_BANDS = [(52_000, 68_000), (68_000, 90_000), (90_000, 120_000),
               (120_000, 155_000), (155_000, 200_000), (200_000, 260_000)]
LEVEL_MIX = {
    "editorial": [0.15, 0.35, 0.30, 0.12, 0.06, 0.02],
    "events": [0.15, 0.35, 0.30, 0.12, 0.06, 0.02],
    "sales": [0.10, 0.30, 0.35, 0.15, 0.08, 0.02],
    "engineering": [0.00, 0.15, 0.30, 0.32, 0.18, 0.05],
    "data": [0.00, 0.15, 0.30, 0.32, 0.18, 0.05],
    "design": [0.00, 0.20, 0.35, 0.28, 0.14, 0.03],
}
DEFAULT_MIX = [0.10, 0.30, 0.30, 0.18, 0.10, 0.02]
PAY_MULTIPLIER = {"engineering": 1.15, "data": 1.15, "design": 1.08}
TITLES = {
    "editorial": ["Editorial assistant", "Staff writer", "Senior writer", "Editor", "Managing editor", "Executive editor"],
    "sales": ["Sales coordinator", "Account executive", "Senior account executive", "Sales manager", "Sales director", "VP, sales"],
    "audience": ["Audience associate", "Audience specialist", "Audience manager", "Senior audience manager", "Audience director", "VP, audience"],
    "subscriptions": ["Subscriptions associate", "Lifecycle specialist", "Subscriptions manager", "Senior subscriptions manager", "Subscriptions director", "VP, consumer revenue"],
    "events": ["Event coordinator", "Event producer", "Senior event producer", "Event manager", "Events director", "VP, events"],
    "engineering": ["Associate engineer", "Software engineer", "Software engineer II", "Senior software engineer", "Staff engineer", "Engineering director"],
    "data": ["Associate data engineer", "Data engineer", "Data engineer II", "Senior data engineer", "Staff data engineer", "Data director"],
    "design": ["Associate designer", "Product designer", "Product designer II", "Senior product designer", "Principal designer", "Design director"],
    "marketing": ["Marketing coordinator", "Marketing specialist", "Marketing manager", "Senior marketing manager", "Marketing director", "VP, marketing"],
    "finance": ["Accounting associate", "Financial analyst", "Senior financial analyst", "Finance manager", "Finance director", "VP, finance"],
    "people": ["People coordinator", "People partner", "Senior people partner", "People manager", "People director", "VP, people"],
    "ga": ["Office coordinator", "Operations specialist", "Senior operations specialist", "Operations manager", "Operations director", "General counsel"],
}

# FY26 hiring plan: (planned start month, level index, days late if filled, or None if open).
NEW_ROLES_2026 = {
    "1110": [(2, 1, 10), (6, 2, 5)],
    "1120": [(9, 1, 7)],
    "1210": [(1, 2, 14), (4, 2, 3), (7, 3, 20)],
    "1310": [(3, 2, 0)],
    "2110": [(1, 1, 21), (5, 2, 9)],
    "2310": [(2, 2, 12), (8, 3, 6)],
    "3110": [(3, 1, 0), (11, 2, None)],
    "3210": [(5, 2, 15)],
    "4110": [(2, 3, 18), (7, 4, None), (8, 3, None), (8, 4, None), (9, 4, None), (11, 3, None)],
    "4120": [(4, 3, 8), (10, 3, None)],
    "4130": [(6, 2, 12)],
    "4210": [(3, 2, 25)],
    "4310": [(1, 2, 10)],
    "4410": [(10, 2, None)],
}

FIRST_NAMES = """Avery Jordan Riley Casey Morgan Quinn Harper Rowan Elliot Skyler Reese Parker Emerson
Finley Hayden Jamie Kendall Logan Micah Noel Peyton Sawyer Tatum Blake Cameron Dakota Drew Hollis
Jules Kai Lane Marlowe Oakley Phoenix Remy Sage Shiloh Sloane Tobin Wren Adele Bruno Celia Dmitri
Esme Felix Greta Hugo Ines Jonah Kira Luca Mina Nico Opal Pavel Rosa Silas Talia Victor""".split()
LAST_NAMES = """Abernathy Bellamy Castellan Draper Ellery Fairchild Galloway Hargrove Iverson Jessup
Kingsley Lockhart Merriweather Northcott Oakes Pendleton Quill Ravenscroft Sterling Thorne Underhill
Vance Whitlock Yardley Ashby Blackwood Carrow Dunmore Everly Fenwick Gilchrist Holloway Ingram Jarrow
Kestrel Langley Marchetti Nightingale Ormsby Prescott Radcliffe Sutherland Tremaine Upton Varga Wexley
Ainsley Brennan Calloway Delacroix Easton Fontaine Grayson Hale Ives Juniper Kavanagh Lindqvist Morrow
Novak""".split()


@dataclass
class Person:
    employee_id: str
    full_name: str
    cc: str
    level: int
    start: date
    term: date | None
    hire_salary: float
    payroll_cc: dict[date, str] = field(default_factory=dict)  # overrides by period

    def salary_at(self, d: date) -> float:
        # Merit increases of 3% every April 1 after the start date.
        aprils = sum(1 for y in range(self.start.year, d.year + 1)
                     if self.start < date(y, 4, 1) <= d)
        return self.hire_salary * 1.03 ** aprils

    def active_days(self, period: date) -> int:
        first, last = period, month_end(period)
        start = max(first, self.start)
        end = min(last, self.term) if self.term else last
        return max(0, (end - start).days + 1)

    def active_at(self, d: date) -> bool:
        return self.start <= d and (self.term is None or self.term >= d)


@dataclass
class Seat:
    cc: str
    level: int
    opened: date
    budget_salary: float = 0.0
    occupants: list[Person] = field(default_factory=list)


@dataclass
class Requisition:
    req_id: str
    cc: str
    level: int
    planned_start: date
    budgeted_salary: float
    filled_by: Person | None


@dataclass
class SourceData:
    entities: list[tuple]
    cost_centers: list[tuple]
    accounts: list[tuple]
    gl_lines: list[tuple]
    trial_balance: list[tuple]
    budget: list[tuple]
    plan_headcount: list[tuple]
    employees: list[tuple]
    requisitions: list[tuple]
    payroll: list[tuple]


class _Names:
    def __init__(self, rng: np.random.Generator):
        combos = [f"{f} {last}" for f, last in itertools.product(FIRST_NAMES, LAST_NAMES)]
        self._names = [combos[i] for i in rng.permutation(len(combos))]
        self._next = 0

    def take(self) -> str:
        name = self._names[self._next]
        self._next += 1
        return name


class Generator:
    def __init__(self, seed: int = SEED):
        self.rng = np.random.default_rng(seed)
        self.names = _Names(self.rng)
        self.people: list[Person] = []
        self.seats: list[Seat] = []
        self.reqs: list[Requisition] = []
        self.periods = all_periods()
        self._emp_seq = itertools.count(10001)

    # People ---------------------------------------------------------------------------

    def _salary(self, family: str, level: int, start: date) -> float:
        lo, hi = LEVEL_BANDS[level]
        base = self.rng.uniform(lo, hi) * PAY_MULTIPLIER.get(family, 1.0)
        # Earlier hires were hired at lower market rates.
        return round(base * 1.03 ** (start.year - BUDGET_YEAR), -2)

    def _new_person(self, cc: str, level: int, start: date, term: date | None) -> Person:
        p = Person(f"E{next(self._emp_seq)}", self.names.take(), cc, level, start, term,
                   self._salary(CC[cc].family, level, start))
        self.people.append(p)
        return p

    def _tenure(self) -> int:
        return int(self.rng.exponential(5 * 365)) + 60

    def _fill_chain(self, seat: Seat, start: date, first_term: date | None = None) -> None:
        term = first_term
        while start <= HORIZON_END:
            if term is None:
                term = start + timedelta(days=self._tenure())
            person = self._new_person(seat.cc, seat.level, start, term if term <= HORIZON_END else None)
            seat.occupants.append(person)
            if person.term is None:
                return
            start = person.term + timedelta(days=int(self.rng.integers(30, 76)))
            term = None

    def build_people(self) -> None:
        for cc in COST_CENTERS:
            mix = LEVEL_MIX.get(cc.family, DEFAULT_MIX)
            new_roles = NEW_ROLES_2026.get(cc.id, [])
            growth = round(0.08 * cc.seats)
            core = cc.seats - len(new_roles) - growth
            for _ in range(core):
                seat = Seat(cc.id, int(self.rng.choice(6, p=mix)), date(2014, 1, 1))
                hired = date(2014, 1, 1) + timedelta(days=int(self.rng.integers(0, 3560)))
                # Tenure is memoryless, so the first occupant's remaining tenure is drawn from 2023-10.
                first_term = FIRST_PERIOD + timedelta(days=self._tenure())
                self._fill_chain(seat, hired, first_term)
                self.seats.append(seat)
            for _ in range(growth):
                opened = add_months(date(2024, 1, 1), int(self.rng.integers(0, 24)))
                opened += timedelta(days=int(self.rng.integers(0, 20)))
                seat = Seat(cc.id, int(self.rng.choice(6, p=mix)), opened)
                self._fill_chain(seat, opened)
                self.seats.append(seat)
            for month, level, late in new_roles:
                planned = date(BUDGET_YEAR, month, 1)
                lo, hi = LEVEL_BANDS[level]
                budgeted = round((lo + hi) / 2 * PAY_MULTIPLIER.get(cc.family, 1.0), -2)
                seat = Seat(cc.id, level, planned)
                filled = None
                if late is not None and planned + timedelta(days=late) <= HORIZON_END:
                    self._fill_chain(seat, planned + timedelta(days=late))
                    filled = seat.occupants[0]
                req = Requisition(f"REQ-26-{len(self.reqs) + 1:03d}", cc.id, level, planned, budgeted, filled)
                self.reqs.append(req)
                if filled is not None:
                    self.seats.append(seat)

        cutoff = date(BUDGET_YEAR - 1, 12, 31)
        for seat in self.seats:
            if seat.opened > cutoff:
                continue
            occupant = next((p for p in seat.occupants if p.active_at(cutoff)), None)
            lo, hi = LEVEL_BANDS[seat.level]
            mid = (lo + hi) / 2 * PAY_MULTIPLIER.get(CC[seat.cc].family, 1.0)
            seat.budget_salary = occupant.salary_at(cutoff) if occupant else mid

    def plant_hr_exceptions(self) -> dict[str, str]:
        """The three September 2026 payroll-vs-HRIS exceptions."""
        sept = LAST_PERIOD
        # 1. Terminated in August, still paid a final check in the September run.
        leaver = next(p for p in self.people if p.cc == "2120" and p.term is None
                      and p.start < date(2025, 1, 1))
        leaver.term = date(2026, 8, 21)
        # 2. Started in September, missing from the September payroll run.
        starter = next(r.filled_by for r in self.reqs if r.cc == "1120" and r.filled_by)
        # 3. Transferred from Data to Platform in HRIS, payroll still charging Data.
        in_data = [p for p in self.people if p.cc == "4120" and p.term is None and p.start < date(2025, 1, 1)]
        mover = next((p for p in in_data if p.level == 3), in_data[0])
        mover.cc = "4110"
        for period in self.periods:
            mover.payroll_cc[period] = "4120"
        return {"paid_after_termination": leaver.employee_id,
                "missing_from_payroll": starter.employee_id,
                "transfer_not_synced": mover.employee_id,
                "period": sept.isoformat()}

    def payroll_register(self, exceptions: dict[str, str]) -> list[tuple]:
        rows = []
        for period in self.periods:
            dim = calendar.monthrange(period.year, period.month)[1]
            for p in self.people:
                days = p.active_days(period)
                if p.employee_id == exceptions["missing_from_payroll"] and period == LAST_PERIOD:
                    continue
                if p.employee_id == exceptions["paid_after_termination"] and period == LAST_PERIOD:
                    rows.append((period, p.employee_id, p.payroll_cc.get(period, p.cc),
                                 round(p.salary_at(date(2026, 8, 21)) / 24, 2)))
                    continue
                if days == 0:
                    continue
                gross = p.salary_at(month_end(period)) / 12 * days / dim
                rows.append((period, p.employee_id, p.payroll_cc.get(period, p.cc), round(gross, 2)))
        return rows

    def heads(self, cc: str, period: date) -> int:
        end = month_end(period)
        return sum(1 for p in self.people if p.cc == cc and p.active_at(end))

    def hires(self, period: date) -> int:
        return sum(1 for p in self.people if p.start.year == period.year and p.start.month == period.month)

    def plan_heads(self, cc: str, period: date) -> float:
        existing = sum(1 for s in self.seats if s.cc == cc and s.opened <= date(BUDGET_YEAR - 1, 12, 31))
        planned = sum(1 for r in self.reqs if r.cc == cc and r.planned_start <= period)
        return existing * VACANCY_FACTOR + planned

    def plan_salaries(self, cc: str, period: date) -> float:
        merit = 1.03 if period.month >= 4 else 1.0
        seats = sum(s.budget_salary for s in self.seats
                    if s.cc == cc and s.opened <= date(BUDGET_YEAR - 1, 12, 31))
        reqs = sum(r.budgeted_salary for r in self.reqs if r.cc == cc and r.planned_start <= period)
        return (seats * merit * VACANCY_FACTOR + reqs) / 12


# Revenue and cost assumptions ------------------------------------------------------------

ADS_SEASON = [0.80, 0.85, 0.95, 0.97, 1.00, 1.00, 0.92, 0.93, 1.02, 1.08, 1.18, 1.30]
COMMERCE_SEASON = [0.75, 0.70, 0.80, 0.85, 0.90, 0.90, 1.00, 0.95, 0.90, 1.00, 1.45, 1.80]
FLAT = [1.0] * 12
EVENT_SEASON = [0, 0, 0, 0.28 * 12, 0, 0.18 * 12, 0, 0, 0.33 * 12, 0, 0.21 * 12, 0]

# (cost center, account): FY26 annual amount, seasonality, yearly growth, noise
REVENUE = {
    ("1210", "4010"): (21_000_000, ADS_SEASON, 1.04, 0.012),
    ("1210", "4020"): (11_000_000, ADS_SEASON, 1.06, 0.012),
    ("1210", "4030"): (4_800_000, ADS_SEASON, 1.10, 0.015),
    ("1310", "4410"): (4_200_000, COMMERCE_SEASON, 1.12, 0.015),
    ("2210", "4010"): (8_400_000, ADS_SEASON, 1.02, 0.012),
    ("2210", "4020"): (6_000_000, ADS_SEASON, 1.05, 0.012),
    ("2210", "4310"): (1_200_000, FLAT, 1.02, 0.02),
    ("2310", "4110"): (13_200_000, FLAT, 1.14, 0.006),
    ("2310", "4120"): (2_600_000, FLAT, 0.90, 0.008),
    ("3110", "4210"): (5_200_000, EVENT_SEASON, 1.06, 0.015),
    ("3210", "4220"): (3_600_000, EVENT_SEASON, 1.06, 0.015),
}


def revenue_drift(cc: str, account: str, period: date) -> float:
    """How far 2026 actuals run from the plan, by line. Zero before 2026.

    The FY26 budget was set in late 2025. Since then the brands business has outrun it,
    the local ad market has softened and event attendance has come in light.
    """
    if period.year != BUDGET_YEAR:
        return 0.0
    m = period.month
    drift = {
        ("1210", "4010"): {6: 0.02, 7: 0.04, 8: 0.06, 9: 0.075}.get(m, 0.0),
        ("1210", "4020"): -0.03 if m >= 3 else 0.0,
        ("1210", "4030"): 0.06 if m >= 4 else 0.0,
        ("1310", "4410"): 0.05 if m >= 3 else 0.0,
        ("2210", "4010"): -0.05 if m >= 3 else 0.0,
        ("2210", "4020"): -0.06 if m >= 2 else 0.0,
        ("2310", "4110"): 0.03 if m >= 6 else 0.0,
        ("2310", "4120"): -0.08 if m >= 4 else 0.0,
        ("3110", "4210"): {4: -0.04, 6: -0.08, 9: -0.08}.get(m, 0.0),
        ("3210", "4220"): 0.04 if m >= 4 else 0.0,
    }
    return drift.get((cc, account), 0.0)


@dataclass(frozen=True)
class Fixed:
    monthly: float
    growth: float
    noise: float
    vendors: tuple[tuple[str, float], ...] = ()
    extra: dict[int, float] | None = None  # month -> one-off amount in the plan


FIXED = {
    ("4110", "5010"): Fixed(185_000, 1.05, 0.02, (("Cirrusvale Cloud", 0.8), ("Farwire CDN", 0.2))),
    ("1110", "5030"): Fixed(42_000, 1.03, 0.04, (("Crestwire News Service", 0.6), ("Pinhole Picture Library", 0.4))),
    ("2110", "5030"): Fixed(58_000, 1.03, 0.04, (("Crestwire News Service", 0.7), ("Pinhole Picture Library", 0.3))),
    ("1110", "5040"): Fixed(40_000, 1.02, 0.05, (("Quillstone Contributor Pay", 1.0),)),
    ("1120", "5040"): Fixed(30_000, 1.02, 0.05, (("Quillstone Contributor Pay", 1.0),)),
    ("2110", "5040"): Fixed(36_000, 1.02, 0.05, (("Quillstone Contributor Pay", 1.0),)),
    ("2120", "5040"): Fixed(26_000, 1.02, 0.05, (("Quillstone Contributor Pay", 1.0),)),
    ("2110", "5130"): Fixed(145_000, 0.92, 0.02, (("Larchmere Print Works", 0.7), ("Porchlight Delivery Co", 0.3))),
    ("4110", "6050"): Fixed(18_000, 1.0, 0.06, (("Hollowpine Staffing", 0.6), ("Brightfold Design Studio", 0.4))),
    ("4120", "6050"): Fixed(10_000, 1.0, 0.06, (("Hollowpine Staffing", 1.0),)),
    ("4130", "6050"): Fixed(8_000, 1.0, 0.06, (("Brightfold Design Studio", 1.0),)),
    ("1310", "6050"): Fixed(6_000, 1.0, 0.06, (("Hollowpine Staffing", 1.0),)),
    ("4120", "7020"): Fixed(62_000, 1.08, 0.03, (("Cirrusvale Cloud", 1.0),)),
    ("4110", "7020"): Fixed(26_000, 1.08, 0.03, (("Cirrusvale Cloud", 1.0),)),
    ("2310", "7040"): Fixed(72_000, 1.05, 0.04, (("Lumenquest Ads", 0.6), ("Socialweave Ads", 0.4))),
    ("1310", "7040"): Fixed(40_000, 1.05, 0.04, (("Lumenquest Ads", 0.5), ("Socialweave Ads", 0.5))),
    ("4210", "7030"): Fixed(35_000, 1.03, 0.04, (("Bright Harbor Creative", 1.0),),
                            {3: 40_000, 9: 60_000}),
    ("4510", "7070"): Fixed(24_000, 1.0, 0.10, (("Hartwell & Lane LLP", 1.0),)),
    ("4310", "7080"): Fixed(12_000, 1.03, 0.03, (("Ashcombe & Pell Partners", 1.0),), {3: 60_000}),
    ("4510", "7090"): Fixed(22_000, 1.04, 0.0, (("Fernhollow Mutual Insurance", 1.0),)),
    ("4510", "7140"): Fixed(140_000, 1.03, 0.0),
    ("4310", "8010"): Fixed(8_000, 1.0, 0.05),
    ("4310", "8020"): Fixed(-12_000, 1.0, 0.0),
    ("4310", "8900"): Fixed(260_000, 1.05, 0.0),
}

PER_HEAD = {  # account: monthly cost per head, vendors
    "6040": (1_150, ()),
    "7010": (190, (("Quillpad Docs", 0.4), ("Pixelmoor Design", 0.3), ("Quillon CRM", 0.3))),
    "7050": (110, ()),
    "7060": (640, ()),
    "7100": (40, ()),
    "7110": (35, ()),
    "7120": (55, ()),
}
SALES_CCS = {"1210": ("4010", "4020", "4030"), "2210": ("4010", "4020"), "3210": ("4220",)}
COMMISSION_RATE = {"1210": 0.03, "2210": 0.03, "3210": 0.05}

OFFSET_ACCOUNT = {
    "4110": "2040", "4120": "2040", "4210": "2050", "4220": "2050",
    "6010": "2030", "6020": "2030", "6030": "2030", "6070": "3020",
    "7130": "1120", "7140": "1320", "8010": "1010", "8020": "1010", "8900": "2020",
    "8100": "1500", "8110": "2500",
}


def _growth_factor(period: date, growth: float) -> float:
    years = (period.year - BUDGET_YEAR) + (period.month - 6.5) / 12
    return growth ** years


def generate(seed: int = SEED, account_catalog: list[tuple[str, str, str]] | None = None) -> SourceData:
    g = Generator(seed)
    g.build_people()
    exceptions = g.plant_hr_exceptions()
    payroll = g.payroll_register(exceptions)
    rng = g.rng

    salaries: dict[tuple[str, date], float] = defaultdict(float)
    for period, _emp, cc, gross in payroll:
        salaries[(cc, period)] += gross

    # amounts[(cc, account, period)] -> list of (vendor, amount) in reporting sign
    actual: dict[tuple[str, str, date], list[tuple[str | None, float]]] = defaultdict(list)
    budget: dict[tuple[str, str, date], float] = defaultdict(float)

    def add(cc: str, account: str, period: date, amount: float, vendor: str | None = None) -> None:
        actual[(cc, account, period)].append((vendor, round(amount, 2)))

    def split(cc, account, period, amount, vendors, noise=0.0):
        if not vendors:
            add(cc, account, period, amount)
            return
        for vendor, share in vendors:
            add(cc, account, period, amount * share * (1 + rng.normal(0, noise)), vendor)

    revenue_actual: dict[tuple[str, str, date], float] = {}
    revenue_plan: dict[tuple[str, str, date], float] = {}
    for period in g.periods:
        for (cc, account), (annual, season, growth, noise) in REVENUE.items():
            expected = annual / 12 * season[period.month - 1] * _growth_factor(period, growth)
            value = expected * (1 + revenue_drift(cc, account, period)) * (1 + rng.normal(0, noise))
            revenue_actual[(cc, account, period)] = value
            revenue_plan[(cc, account, period)] = expected
            add(cc, account, period, value)
        for cc in ("1210", "2210"):
            add(cc, "4900", period, -0.15 * revenue_actual[(cc, "4010", period)])

    def ads(src, cc, period):
        return sum(src[(cc, a, period)] for a in ("4010", "4020", "4030") if (cc, a, period) in src)

    for period in g.periods:
        # Cost of revenue driven by revenue.
        for cc in ("1210", "2210"):
            split(cc, "5020", period, 0.03 * ads(revenue_actual, cc, period), (("Tessellate Ad Server", 1.0),))
        subs_a = revenue_actual[("2310", "4110", period)] + revenue_actual[("2310", "4120", period)]
        add("2310", "5050", period, 0.03 * subs_a)
        tickets_a = revenue_actual[("3110", "4210", period)]
        add("3110", "5050", period, 0.03 * tickets_a)
        tickets_p = revenue_plan[("3110", "4210", period)]
        if tickets_p:
            venue = 0.35 * tickets_p * (1 + rng.normal(0, 0.02))
            if period == LAST_PERIOD:
                venue += 42_000  # security and catering overrun at the September event
            split("3110", "5110", period, venue,
                  (("Gullhaven Convention Center", 0.8), ("Watchcrest Security Services", 0.2)))
            split("3110", "5120", period, 0.22 * tickets_p * (1 + rng.normal(0, 0.02)),
                  (("Stagewright Productions", 1.0),))

        # Fixed and vendor-driven lines.
        for (cc, account), f in FIXED.items():
            base = f.monthly * _growth_factor(period, f.growth)
            extra = (f.extra or {}).get(period.month, 0.0)
            if (cc, account) == ("4210", "7030") and period.year == BUDGET_YEAR:
                # The fall brand event was invoiced in August, a month before the plan.
                if period.month == 8:
                    split(cc, account, period, base, f.vendors, f.noise)
                    add(cc, account, period, 62_000, "Lanternfish Events")
                elif period.month == 9:
                    split(cc, account, period, base, f.vendors, f.noise)
                else:
                    split(cc, account, period, base + extra, f.vendors, f.noise)
            else:
                split(cc, account, period, base + extra, f.vendors, f.noise)
        if period >= date(2026, 7, 1):
            # A contractor firm covering the open Platform roles.
            add("4110", "6050", period, {7: 26_000, 8: 37_000, 9: 44_000}[period.month],
                "Kettlebrook Contract Engineering")

        # Intercompany shared services fee, not budgeted, eliminated in consolidation.
        add("4510", "8100", period, 120_000)
        add("3110", "8110", period, -120_000)

        # People costs.
        for cc in COST_CENTERS:
            s = salaries[(cc.id, period)]
            add(cc.id, "6010", period, s)
            bonus = 0.08 * s
            commission = 0.0
            if cc.id in SALES_CCS:
                commission = COMMISSION_RATE[cc.id] * sum(revenue_actual[(cc.id, a, period)] for a in SALES_CCS[cc.id])
            add(cc.id, "6020", period, bonus + commission)
            add(cc.id, "6030", period, 0.07 * (s + bonus + commission))
            add(cc.id, "6070", period, 0.025 * s)
            heads = g.heads(cc.id, period)
            for account, (per_head, vendors) in PER_HEAD.items():
                rate = per_head * (3 if account == "7050" and cc.family == "sales" else 1)
                split(cc.id, account, period, heads * rate * (1 + rng.normal(0, 0.02)), vendors, 0.03)

        hires = g.hires(period)
        split("4410", "6060", period, 3_000 + 3_500 * hires, (("Fernbrook Talent Search", 1.0),))
        ads_total_a = sum(ads(revenue_actual, cc, period) for cc in ("1210", "2210"))
        add("4310", "7130", period, 0.005 * ads_total_a)

    # The FY26 budget covers the whole year, including months that have not closed yet.
    for month in range(1, 13):
        _plan_month(g, date(BUDGET_YEAR, month, 1), budget)
    return _assemble(g, actual, budget, payroll, rng, account_catalog)


def _plan_month(g: Generator, period: date, budget: dict) -> None:
    """The budget for one month, from the plan assumptions alone (no noise, no drift)."""
    rev = {}
    for (cc, account), (annual, season, growth, _noise) in REVENUE.items():
        rev[(cc, account)] = annual / 12 * season[period.month - 1] * _growth_factor(period, growth)
        budget[(cc, account, period)] += rev[(cc, account)]
    ads = {cc: sum(rev.get((cc, a), 0.0) for a in ("4010", "4020", "4030")) for cc in ("1210", "2210")}
    for cc in ("1210", "2210"):
        budget[(cc, "4900", period)] += -0.15 * rev[(cc, "4010")]
        budget[(cc, "5020", period)] += 0.03 * ads[cc]
    budget[("2310", "5050", period)] += 0.03 * (rev[("2310", "4110")] + rev[("2310", "4120")])
    tickets = rev[("3110", "4210")]
    budget[("3110", "5050", period)] += 0.03 * tickets
    if tickets:
        budget[("3110", "5110", period)] += 0.35 * tickets
        budget[("3110", "5120", period)] += 0.22 * tickets
    for (cc, account), f in FIXED.items():
        budget[(cc, account, period)] += (f.monthly * _growth_factor(period, f.growth)
                                          + (f.extra or {}).get(period.month, 0.0))
    for cc in COST_CENTERS:
        ps = g.plan_salaries(cc.id, period)
        pheads = g.plan_heads(cc.id, period)
        pcomm = (COMMISSION_RATE[cc.id] * sum(rev[(cc.id, a)] for a in SALES_CCS[cc.id])
                 if cc.id in SALES_CCS else 0.0)
        budget[(cc.id, "6010", period)] += ps
        budget[(cc.id, "6020", period)] += 0.08 * ps + pcomm
        budget[(cc.id, "6030", period)] += 0.07 * (ps * 1.08 + pcomm)
        budget[(cc.id, "6070", period)] += 0.025 * ps
        for account, (per_head, _v) in PER_HEAD.items():
            rate = per_head * (3 if account == "7050" and cc.family == "sales" else 1)
            budget[(cc.id, account, period)] += pheads * rate
    planned_starts = sum(1 for r in g.reqs if r.planned_start == period)
    expected_backfills = 0.2 / 12 * sum(c.seats for c in COST_CENTERS)
    budget[("4410", "6060", period)] += 3_000 + 3_500 * (planned_starts + expected_backfills)
    budget[("4310", "7130", period)] += 0.005 * (ads["1210"] + ads["2210"])


def _assemble(g: Generator, actual, budget, payroll, rng, account_catalog) -> SourceData:
    from fpa.semantic import load_catalog

    catalog = load_catalog()
    line_ids = itertools.count(1)
    gl: list[tuple] = []
    journal_seq: dict[tuple[str, date], int] = defaultdict(int)
    bs_activity: dict[tuple[str, date, str], float] = defaultdict(float)

    def journal(entity: str, period: date) -> str:
        journal_seq[(entity, period)] += 1
        return f"JE-{entity}-{period:%Y%m}-{journal_seq[(entity, period)]:05d}"

    def line(jid, entity, period, posted, account, cc, vendor, memo, debit_amount):
        amount = round(debit_amount, 2)
        if amount == 0:
            return
        gl.append((next(line_ids), jid, entity, period, posted, account, cc, vendor, memo, amount))
        if catalog.accounts[account].statement == "balance_sheet":
            bs_activity[(entity, period, account)] += amount

    for (cc, account, period), parts in sorted(actual.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
        entity = CC[cc].entity
        sign = catalog.accounts[account].reporting_sign
        offset = OFFSET_ACCOUNT.get(account, "1110" if account.startswith("4") else "2010")
        for vendor, amount in parts:
            posted = (date(period.year, period.month, int(rng.integers(3, 27))) if vendor
                      else month_end(period))
            memo = (f"{vendor} invoice {period:%Y%m}-{int(rng.integers(1000, 9999))}" if vendor
                    else f"{catalog.accounts[account].name}, {period:%B %Y}")
            jid = journal(entity, period)
            debit = amount * sign
            line(jid, entity, period, posted, account, cc, vendor, memo, debit)
            line(jid, entity, period, posted, offset, None, None, memo, -debit)

    # Each month settles the previous month's balance-sheet activity through cash.
    settle_skip = {"1010", "1120", "1320", "3020", "1500", "2500"}
    for period in g.periods[1:]:
        prev = add_months(period, -1)
        for entity, _name, _ccy in ENTITIES:
            jid = journal(entity, period)
            cash = 0.0
            for (e, p, account), amount in sorted(bs_activity.items()):
                if e != entity or p != prev or account in settle_skip or round(amount, 2) == 0:
                    continue
                gl.append((next(line_ids), jid, entity, period, date(period.year, period.month, 10),
                           account, None, None, "Monthly settlement", round(-amount, 2)))
                cash += round(amount, 2)
            if cash:
                gl.append((next(line_ids), jid, entity, period, date(period.year, period.month, 10),
                           "1010", None, None, "Monthly settlement", round(cash, 2)))

    tb: dict[tuple[str, date, str], float] = defaultdict(float)
    for row in gl:
        tb[(row[2], row[3], row[5])] += row[9]
    trial_balance = [(e, p, a, round(v, 2)) for (e, p, a), v in sorted(tb.items())]

    budget_rows = [(BUDGET_VERSION, p, cc, a, round(v, 2))
                   for (cc, a, p), v in sorted(budget.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1]))]

    employees = [(p.employee_id, p.full_name, p.cc, TITLES[CC[p.cc].family][p.level], f"L{p.level + 1}",
                  p.start, p.term, round(p.salary_at(p.term or HORIZON_END), 2)) for p in g.people]
    reqs = [(r.req_id, r.cc, TITLES[CC[r.cc].family][r.level], f"L{r.level + 1}", r.planned_start,
             r.budgeted_salary, "filled" if r.filled_by else "open",
             r.filled_by.employee_id if r.filled_by else None) for r in g.reqs]

    # The planning workbook's headcount plan: budgeted positions less the vacancy allowance.
    plan_headcount = [(BUDGET_VERSION, date(BUDGET_YEAR, m, 1), c.id, round(g.plan_heads(c.id, date(BUDGET_YEAR, m, 1)), 2))
                      for m in range(1, 13) for c in COST_CENTERS]

    accounts = account_catalog or [
        (a.number, a.name, a.section.name) for a in catalog.accounts.values()
    ]
    return SourceData(
        entities=list(ENTITIES),
        cost_centers=[(c.id, c.name, c.dept_id, c.dept_name, c.bu_id, c.bu_name, c.entity) for c in COST_CENTERS],
        accounts=accounts,
        gl_lines=gl,
        trial_balance=trial_balance,
        budget=budget_rows,
        plan_headcount=plan_headcount,
        employees=employees,
        requisitions=reqs,
        payroll=payroll,
    )


# People who use the system --------------------------------------------------------------

GRANTOR = "sam.keller@example.com"
PEOPLE = [
    # person, name, title, IdP groups, grants (scope_type, scope_value, reason, expires_at)
    ("maya.ortiz@example.com", "Maya Ortiz", "Chief Financial Officer", ["finance-leadership"],
     [("all", None, "CFO", None)]),
    ("sam.keller@example.com", "Sam Keller", "FP&A Lead", ["fpa", "fpa-admins"],
     [("all", None, "Owns the monthly package and the access review", None)]),
    ("priya.nair@example.com", "Priya Nair", "Senior FP&A Analyst", ["fpa"],
     [("all", None, "Prepares the monthly package", None)]),
    ("tom.reyes@example.com", "Tom Reyes", "Controller", ["accounting"],
     [("all", None, "Owns the close and the tie-out", None)]),
    ("dana.whitfield@example.com", "Dana Whitfield", "VP, Engineering", ["budget-owners"],
     [("department", "CORP-ENG", "Budget owner for Engineering", None)]),
    ("omar.haddad@example.com", "Omar Haddad", "Platform Engineering Manager", ["budget-owners"],
     [("cost_center", "4110", "Manages the Platform engineering cost center", None)]),
    ("leo.marsh@example.com", "Leo Marsh", "GM, Local News", ["budget-owners"],
     [("business_unit", "LN", "General manager of Local News", None)]),
    ("nina.cho@example.com", "Nina Cho", "VP, Brands Ad Sales", ["budget-owners"],
     [("department", "CB-SAL", "Budget owner for Brands Ad Sales", None)]),
    ("grace.lin@example.com", "Grace Lin", "Head of Marketing", ["budget-owners"],
     [("department", "CORP-MKT", "Budget owner for Marketing", None)]),
    ("ruth.ellison@example.com", "Ruth Ellison", "Head of Live Events", ["budget-owners"],
     [("business_unit", "LE", "Runs Live Events", None)]),
    ("hana.kowalski@example.com", "Hana Kowalski", "External Auditor", ["auditors"],
     [("all", None, "Year-end audit fieldwork", "2026-09-30T23:59:59+00:00")]),
    ("ben.foster@example.com", "Ben Foster", "Business Analyst", ["staff"], []),
]
GROUP_PERMISSIONS = [
    ("fpa", "review_variances"),
    ("fpa", "comp_detail"),
    ("finance-leadership", "comp_detail"),
    ("fpa-admins", "admin_access"),
]
