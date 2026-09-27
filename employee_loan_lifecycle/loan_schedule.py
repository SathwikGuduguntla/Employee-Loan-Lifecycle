"""Repayment schedule maths.

Pure functions, no Frappe imports, so the arithmetic can be unit-tested in
isolation and reasoned about line by line. The Loan controller owns *when*
a schedule is (re)built; this module only owns *what* the rows are.

Rounding: every monetary figure is rounded half-up to ``precision`` decimal
places. The caller passes the precision it reads from the company's currency
settings; nothing here hard-codes 2.

Closing rule: every period's principal comes from the formula except the
last one, which takes whatever principal is left. That is what guarantees
the principal column sums *exactly* to the amount being scheduled and the
final closing balance is exactly zero, however rounding drifted on the way.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

FLAT = "Flat"
REDUCING = "Reducing Balance"


def _q(precision: int) -> Decimal:
    return Decimal(1).scaleb(-precision)


def money(value, precision: int) -> Decimal:
    return Decimal(str(value)).quantize(_q(precision), rounding=ROUND_HALF_UP)


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, months: int) -> date:
    """First day of the month ``months`` after ``d``'s month."""
    index = d.year * 12 + (d.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def emi(principal: Decimal, monthly_rate: Decimal, periods: int) -> Decimal:
    """Standard amortising instalment, unrounded."""
    if monthly_rate == 0:
        return principal / periods
    factor = (1 + monthly_rate) ** periods
    return principal * monthly_rate * factor / (factor - 1)


def build_rows(
    principal,
    annual_rate_percent,
    periods: int,
    interest_mode: str,
    first_due_month: date,
    precision: int = 2,
    flat_interest_base=None,
    start_period_no: int = 1,
) -> list[dict]:
    """Return schedule rows for ``principal`` over ``periods`` months.

    ``first_due_month`` is any date in the first repayment month; each row's
    ``due_date`` is the last day of its month, i.e. the payroll period the
    instalment belongs to.

    ``flat_interest_base`` lets a rebuild after a tranche keep charging flat
    interest on the whole disbursed principal (not only on what is left to
    schedule). It defaults to ``principal``.
    """
    if periods < 1:
        raise ValueError("A schedule needs at least one period.")
    if interest_mode not in (FLAT, REDUCING):
        raise ValueError(f"Unknown interest mode: {interest_mode}")

    principal = money(principal, precision)
    if principal <= 0:
        return []

    monthly_rate = Decimal(str(annual_rate_percent or 0)) / Decimal(1200)
    rows = []
    opening = principal

    if interest_mode == FLAT:
        base = money(flat_interest_base if flat_interest_base is not None else principal, precision)
        flat_interest = money(base * monthly_rate, precision)
        flat_principal = money(principal / periods, precision)
    else:
        instalment = money(emi(principal, monthly_rate, periods), precision)

    for i in range(periods):
        last = i == periods - 1

        if interest_mode == FLAT:
            interest = flat_interest
            principal_part = opening if last else min(flat_principal, opening)
        else:
            interest = money(opening * monthly_rate, precision)
            principal_part = opening if last else min(money(instalment - interest, precision), opening)

        closing = opening - principal_part
        rows.append(
            {
                "period_no": start_period_no + i,
                "due_date": month_end(add_months(first_due_month, i)),
                "opening_balance": opening,
                "interest_amount": interest,
                "principal_amount": principal_part,
                "instalment_amount": interest + principal_part,
                "closing_balance": closing,
            }
        )
        opening = closing

    return rows
