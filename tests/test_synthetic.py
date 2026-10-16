from fpa import synthetic


def test_generation_is_deterministic():
    a, b = synthetic.generate(), synthetic.generate()
    assert a.gl_lines[:50] == b.gl_lines[:50] and len(a.gl_lines) == len(b.gl_lines)
    assert sum(r[9] for r in a.gl_lines if r[5] == "6010") == sum(r[9] for r in b.gl_lines if r[5] == "6010")


def test_salaries_in_the_gl_tie_to_the_payroll_register(owner):
    rows = owner.execute(
        """
        with gl as (select period, cost_center_id, sum(amount) as amount from src.erp_gl_lines
                    where account_number = '6010' group by 1, 2),
        pay as (select period, cost_center_id, sum(gross_pay) as amount from src.payroll_register group by 1, 2)
        select count(*) as n from gl full join pay using (period, cost_center_id)
        where coalesce(gl.amount, 0) <> coalesce(pay.amount, 0)
        """
    ).fetchone()
    assert rows["n"] == 0


def test_only_synthetic_contact_details():
    names = {p[0] for p in synthetic.PEOPLE}
    assert all(n.endswith("@example.com") for n in names)
