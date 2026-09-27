# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt


import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_first_day, getdate, nowdate


# ---------------- Schedule maths (pure, no Frappe) ----------------
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
    """Schedule rows for ``principal`` over ``periods`` months. due_date is the
    last day of each month. The last row takes whatever principal is left, so
    the principal column sums exactly and the final closing balance is 0."""
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


# ---------------- Loan ----------------

LIVE_STATUSES = ("Partially Disbursed", "Disbursed", "Repaying")
READ_PTYPES = ("read", "select", "print", "email")
ALL_SEEING_ROLES = ("System Manager", "Finance Manager", "CFO")


class Loan(Document):
    # ---------------- Lifecycle ----------------

    def validate(self):
        self.validate_terms()
        if self.docstatus == 0:
            self.first_deduction_month = get_first_day(self.first_deduction_month)
            self.status = "Sanctioned"
            self.disbursed_amount = 0
            # Projection on the sanctioned amount; rebuilt on the disbursed
            # amount when the first tranche is released.
            self.set("repayment_schedule", [])
            self._append_rows(self._build(self.sanctioned_amount, self.tenure_months, self.first_deduction_month))
            self.refresh_balances(persist=False)

    def validate_terms(self):
        if flt(self.sanctioned_amount) <= 0:
            frappe.throw(_("Sanctioned amount must be greater than zero."))
        if not self.tenure_months or self.tenure_months < 1:
            frappe.throw(_("Tenure must be at least one month."))
        if flt(self.rate_of_interest) < 0:
            frappe.throw(_("Rate of interest cannot be negative."))

    def before_cancel(self):
        if flt(self.disbursed_amount) > 0:
            frappe.throw(_("Cancel the loan's disbursements before cancelling the loan."))

    # ---------------- Schedule ----------------

    @property
    def precision_digits(self) -> int:
        return self.precision("principal_amount", "repayment_schedule") or 2

    def _build(self, principal, periods, first_month, start_period_no=1):
        return build_rows(
            principal=flt(principal),
            annual_rate_percent=flt(self.rate_of_interest),
            periods=max(int(periods), 1),
            interest_mode=self.interest_mode,
            first_due_month=getdate(first_month),
            precision=self.precision_digits,
            flat_interest_base=flt(self.basis_amount()),
            start_period_no=start_period_no,
        )

    def _append_rows(self, rows):
        appended = []
        for row in rows:
            appended.append(
                self.append(
                    "repayment_schedule",
                    {
                        **{k: (flt(v) if not isinstance(v, (int, date)) else v) for k, v in row.items()},
                        "row_type": "Instalment",
                        "recovered_amount": 0,
                        "principal_recovered": 0,
                        "interest_recovered": 0,
                        "status": "Pending",
                    },
                )
            )
        return appended

    def basis_amount(self):
        """What the schedule is built on: disbursed money once any has moved."""
        return flt(self.disbursed_amount) or flt(self.sanctioned_amount)

    def rows(self):
        """Rows in period order. period_no never changes once a row is settled,
        because salary slips and repayments point at it."""
        return sorted(self.repayment_schedule, key=lambda r: (r.period_no, r.idx))

    def next_period_no(self, rows=None):
        rows = self.repayment_schedule if rows is None else rows
        return max([r.period_no for r in rows] or [0]) + 1

    def is_settled(self, row, as_of, by_date=True):
        return (
            flt(row.recovered_amount) > 0
            or row.row_type == "Prepayment"
            or (by_date and getdate(row.due_date) < get_first_day(as_of))
        )

    def rebuild_future_schedule(self, as_of, reason, by_date=True):
        """Regenerate unsettled rows so principal again sums exactly to
        basis_amount(). by_date=False on the first tranche: before money is
        lent, a projected month that has passed is not an arrear."""
        as_of = getdate(as_of or nowdate())
        rows = self.rows()
        settled = [r for r in rows if self.is_settled(r, as_of, by_date)]
        future = [r for r in rows if not self.is_settled(r, as_of, by_date)]

        if settled and future and future[0].period_no < settled[-1].period_no:
            frappe.throw(_("Loan {0}: schedule has recovered rows after unrecovered future rows.").format(self.name))

        scheduled = sum(flt(r.principal_amount) for r in settled)
        remaining = flt(self.basis_amount() - scheduled, self.precision_digits)
        if remaining < 0:
            frappe.throw(
                _("Loan {0}: principal already scheduled in settled periods ({1}) exceeds the amount lent ({2}).").format(
                    self.name, scheduled, self.basis_amount()
                )
            )

        instalments_used = len([r for r in settled if r.row_type != "Prepayment"])
        periods_left = max(int(self.tenure_months) - instalments_used, 1)

        first_month = getdate(self.first_deduction_month)
        if settled:
            first_month = max(first_month, add_months(max(getdate(r.due_date) for r in settled), 1))
        first_month = max(first_month, get_first_day(as_of)) if flt(self.disbursed_amount) else first_month

        old = [(r.period_no, flt(r.instalment_amount)) for r in future]
        for r in future:
            self.remove(r)
            if not r.is_new():
                frappe.db.delete("Loan Repayment Schedule", {"name": r.name})

        new_rows = self._append_rows(
            self._build(remaining, periods_left, first_month, start_period_no=self.next_period_no(settled))
        )
        for i, r in enumerate(self.rows(), start=1):
            r.idx = i

        if self.docstatus == 1:
            for r in self.repayment_schedule:
                if r in new_rows:
                    r.db_insert()
                else:
                    r.db_update()
            self.add_comment(
                "Info",
                _("Schedule rebuilt ({0}): {1} future period(s) replaced; {2} principal re-spread over {3} period(s) from {4}.").format(
                    reason, len(old), remaining, periods_left, first_month.strftime("%b %Y")
                ),
            )

    # ---------------- Balances and status ----------------

    def refresh_balances(self, persist=True, as_of=None):
        as_of = getdate(as_of or nowdate())
        p = self.precision_digits
        rows = self.repayment_schedule

        self.principal_recovered = flt(sum(flt(r.principal_recovered) for r in rows), p)
        self.interest_recovered = flt(sum(flt(r.interest_recovered) for r in rows), p)
        self.outstanding_principal = flt(flt(self.disbursed_amount) - self.principal_recovered, p)
        # Interest is recognised in the ledger when it is recovered, so nothing
        # accrued-but-uncollected sits in the receivable. The scheduled accrual
        # job (stretch) would populate this.
        self.outstanding_interest = 0
        self.arrears_amount = flt(
            sum(
                flt(r.instalment_amount) - flt(r.recovered_amount)
                for r in rows
                if getdate(r.due_date) < get_first_day(as_of) and r.status != "Recovered"
            ),
            p,
        )
        self.status = self.compute_status()
        if self.status == "Closed":
            self.closure_date = self.closure_date or as_of
        else:
            self.closure_date = None

        if persist:
            self.db_update()

    def compute_status(self):
        if self.status == "Written Off":
            return "Written Off"
        disbursed = flt(self.disbursed_amount)
        if disbursed <= 0:
            return "Sanctioned"
        if self.outstanding_principal <= 0 and all(r.status == "Recovered" for r in self.repayment_schedule):
            return "Closed"
        if flt(self.principal_recovered) > 0 or flt(self.interest_recovered) > 0:
            return "Repaying"
        if disbursed < flt(self.sanctioned_amount):
            return "Partially Disbursed"
        return "Disbursed"

    # ---------------- Money movements ----------------
    # Callers must be inside the transaction that also posts the ledger entry.

    def lock(self):
        frappe.db.sql("select name from `tabLoan` where name = %s for update", self.name)
        self.reload()
        return self

    def disbursed_total(self):
        return flt(
            frappe.db.sql(
                "select coalesce(sum(amount), 0) from `tabLoan Disbursement` where loan = %s and docstatus = 1",
                self.name,
            )[0][0]
        )

    def apply_disbursement(self, as_of):
        """Called from Loan Disbursement.on_submit."""
        first_tranche = flt(self.disbursed_amount) <= 0
        self.disbursed_amount = self.disbursed_total()
        if self.disbursed_amount > flt(self.sanctioned_amount):
            frappe.throw(
                _("Total disbursed ({0}) would exceed the sanctioned amount ({1}).").format(
                    self.disbursed_amount, self.sanctioned_amount
                )
            )
        self.rebuild_future_schedule(as_of, _("tranche released"), by_date=not first_tranche)
        self.refresh_balances(as_of=as_of)

    def reverse_disbursement(self, as_of):
        """Called from Loan Disbursement.on_cancel."""
        self.disbursed_amount = self.disbursed_total()
        if flt(self.principal_recovered) > self.disbursed_amount:
            frappe.throw(
                _(
                    "Cannot cancel: {0} of principal has already been recovered but only {1} would remain disbursed. "
                    "Cancel the recoveries first."
                ).format(self.principal_recovered, self.disbursed_amount)
            )
        if self.disbursed_amount:
            self.rebuild_future_schedule(as_of, _("tranche cancelled"))
        else:
            # Nothing left disbursed: go back to the projection on sanctioned amount.
            for r in list(self.repayment_schedule):
                if flt(r.recovered_amount) == 0:
                    self.remove(r)
                    frappe.db.delete("Loan Repayment Schedule", {"name": r.name})
            if not self.repayment_schedule:
                for r in self._append_rows(
                    self._build(self.sanctioned_amount, self.tenure_months, self.first_deduction_month)
                ):
                    r.db_insert()
        self.refresh_balances(as_of=as_of)

    def get_row(self, period_no):
        for r in self.repayment_schedule:
            if r.period_no == int(period_no):
                return r
        frappe.throw(_("Loan {0} has no schedule period {1}.").format(self.name, period_no))

    def apply_recovery(self, period_no, principal, interest, as_of=None):
        row = self.get_row(period_no)
        p = self.precision_digits
        principal, interest = flt(principal, p), flt(interest, p)
        if principal < 0 or interest < 0:
            frappe.throw(_("Recovery amounts cannot be negative."))
        if flt(row.principal_recovered) + principal > flt(row.principal_amount) + 0.001 or (
            flt(row.interest_recovered) + interest > flt(row.interest_amount) + 0.001
        ):
            frappe.throw(
                _("Loan {0} period {1}: this recovery would take more than is due. It may already have been recovered.").format(
                    self.name, period_no
                )
            )
        row.principal_recovered = flt(flt(row.principal_recovered) + principal, p)
        row.interest_recovered = flt(flt(row.interest_recovered) + interest, p)
        self._set_row_status(row)
        row.db_update()
        self.refresh_balances(as_of=as_of)

    def reverse_recovery(self, period_no, principal, interest, as_of=None):
        row = self.get_row(period_no)
        p = self.precision_digits
        principal, interest = flt(principal, p), flt(interest, p)
        if principal - flt(row.principal_recovered) > 0.001 or interest - flt(row.interest_recovered) > 0.001:
            frappe.throw(_("Loan {0} period {1}: cannot reverse more than was recovered.").format(self.name, period_no))
        row.principal_recovered = flt(flt(row.principal_recovered) - principal, p)
        row.interest_recovered = flt(flt(row.interest_recovered) - interest, p)
        self._set_row_status(row)
        row.db_update()
        self.refresh_balances(as_of=as_of)

    def _set_row_status(self, row):
        row.recovered_amount = flt(flt(row.principal_recovered) + flt(row.interest_recovered), self.precision_digits)
        if row.recovered_amount <= 0:
            row.status = "Pending"
        elif row.recovered_amount + 0.001 >= flt(row.instalment_amount):
            row.status = "Recovered"
        else:
            row.status = "Partially Recovered"

    # ---------------- Allocation (shared by payroll and direct repayment) ----------------

    def due_rows(self, up_to):
        up_to = getdate(up_to)
        return [
            r
            for r in self.rows()
            if r.row_type != "Prepayment" and r.status != "Recovered" and getdate(r.due_date) <= up_to
        ]

    def allocate(self, amount, up_to, current_from):
        """Split ``amount`` over rows due on or before ``up_to``:
        1. interest on every due row, oldest first
        2. principal of the current period (due on/after ``current_from``)
        3. arrears: principal of earlier periods, oldest first
        Returns ([{row, interest, principal, is_arrears}], unallocated)."""
        p = self.precision_digits
        remaining = flt(amount, p)
        current_from = getdate(current_from)
        rows = self.due_rows(up_to)
        alloc = {
            r.period_no: {"row": r, "interest": 0.0, "principal": 0.0, "is_arrears": getdate(r.due_date) < current_from}
            for r in rows
        }

        def take(r, due_field, done_field, key):
            nonlocal remaining
            due = flt(flt(r.get(due_field)) - flt(r.get(done_field)), p)
            amount_ = min(due, remaining)
            if amount_ > 0:
                alloc[r.period_no][key] = flt(alloc[r.period_no][key] + amount_, p)
                remaining = flt(remaining - amount_, p)

        for r in rows:
            take(r, "interest_amount", "interest_recovered", "interest")
        for r in [r for r in rows if not alloc[r.period_no]["is_arrears"]]:
            take(r, "principal_amount", "principal_recovered", "principal")
        for r in [r for r in rows if alloc[r.period_no]["is_arrears"]]:
            take(r, "principal_amount", "principal_recovered", "principal")

        return [a for a in alloc.values() if a["interest"] or a["principal"]], remaining

    def future_principal(self, as_of):
        """Principal on rows not yet due: what a prepayment can reduce."""
        return flt(
            sum(
                flt(r.principal_amount) - flt(r.principal_recovered)
                for r in self.rows()
                if r.row_type != "Prepayment" and getdate(r.due_date) > getdate(as_of)
            ),
            self.precision_digits,
        )

    def apply_prepayment(self, amount, as_of):
        """Recorded as its own settled row (so principal still totals the
        amount lent), then the future is re-amortised over the remaining
        tenure: the instalment falls, the end date stays."""
        as_of = getdate(as_of)
        amount = flt(amount, self.precision_digits)
        available = self.future_principal(as_of)
        if amount <= 0 or amount - available > 0.001:
            frappe.throw(_("Prepayment of {0} exceeds the {1} of principal not yet due.").format(amount, available))
        row = self.append(
            "repayment_schedule",
            {
                "row_type": "Prepayment",
                "period_no": self.next_period_no(),
                "due_date": as_of,
                "opening_balance": available,
                "interest_amount": 0,
                "principal_amount": amount,
                "instalment_amount": amount,
                "closing_balance": flt(available - amount, self.precision_digits),
                "principal_recovered": amount,
                "interest_recovered": 0,
                "recovered_amount": amount,
                "status": "Recovered",
            },
        )
        row.db_insert()
        self.rebuild_future_schedule(as_of, _("prepayment of {0}").format(amount))
        self.refresh_balances(as_of=as_of)
        return row.period_no

    def reverse_prepayment(self, period_no, as_of=None):
        row = self.get_row(period_no)
        if row.row_type != "Prepayment":
            frappe.throw(_("Period {0} is not a prepayment.").format(period_no))
        later = [r.period_no for r in self.repayment_schedule if r.period_no > row.period_no and flt(r.recovered_amount) > 0]
        if later:
            frappe.throw(
                _(
                    "Cannot reverse the prepayment: period(s) {0} of the re-amortised schedule have since been "
                    "recovered. Cancel those recoveries first."
                ).format(", ".join(map(str, later)))
            )
        self.remove(row)
        frappe.db.delete("Loan Repayment Schedule", {"name": row.name})
        self.rebuild_future_schedule(as_of or nowdate(), _("prepayment reversed"))
        self.refresh_balances(as_of=as_of)


# ---------------- Row-level access control (wired in hooks.py) ----------------


def _employee_for(user):
    return frappe.db.get_value("Employee", {"user_id": user}, ["name", "lft", "rgt", "department"], as_dict=True)


def get_permission_query_conditions(user=None, doctype="Loan"):
    user = user or frappe.session.user
    roles = frappe.get_roles(user)
    if user == "Administrator" or any(r in roles for r in ALL_SEEING_ROLES):
        return ""

    table = f"`tab{doctype}`"
    emp = _employee_for(user)
    conditions = []
    if emp:
        conditions.append(f"{table}.employee = {frappe.db.escape(emp.name)}")
        if emp.lft and emp.rgt:
            # Employee is a nested set on reports_to: all direct and indirect reports.
            conditions.append(
                f"{table}.employee in (select name from `tabEmployee` where lft > {int(emp.lft)} and rgt < {int(emp.rgt)})"
            )
        if "HR Manager" in roles and emp.department:
            conditions.append(
                f"{table}.employee in (select name from `tabEmployee` where department = {frappe.db.escape(emp.department)})"
            )
    if "Treasury Officer" in roles and doctype == "Loan":
        conditions.append(f"{table}.status in ('Sanctioned', 'Partially Disbursed')")
    return "(" + " or ".join(conditions) + ")" if conditions else "1=0"


def has_permission(doc, ptype="read", user=None, debug=False):
    user = user or frappe.session.user
    roles = frappe.get_roles(user)
    if user == "Administrator" or any(r in roles for r in ALL_SEEING_ROLES):
        return True

    emp = _employee_for(user)
    if emp:
        if doc.employee == emp.name:
            return ptype in READ_PTYPES
        target = frappe.db.get_value("Employee", doc.employee, ["lft", "rgt", "department"], as_dict=True)
        if target and emp.lft and target.lft > emp.lft and target.rgt < emp.rgt:
            return ptype in READ_PTYPES
        if target and "HR Manager" in roles and emp.department and target.department == emp.department:
            return ptype in READ_PTYPES
    if "Treasury Officer" in roles and doc.get("status") in ("Sanctioned", "Partially Disbursed"):
        return ptype in ("read", "select")
    return False