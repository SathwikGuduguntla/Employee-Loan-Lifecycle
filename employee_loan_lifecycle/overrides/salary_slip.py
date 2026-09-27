"""Salary Slip: loan instalments recovered through payroll.

Wired with ``override_doctype_class`` (see DECISIONS.md §6 for why this and
not doc_events). HRMS already reserves the place for this: without the
lending app installed, ``SalarySlip.set_net_pay`` still computes

    net_pay = gross_pay - (total_deduction + total_loan_repayment)

and Payroll Entry's bank entry pays ``... - total_loan_repayment``. This app
adds the ``total_loan_repayment`` field and fills it, so net pay, the bank
payment and the payslip all agree without touching HRMS code.

Rule for a month where net pay cannot cover the instalment (DECISIONS.md §7):
recover what fits, never push net pay below zero. Interest before principal,
oldest period first, older loans first. What is not recovered stays on its
schedule row and is due again (as arrears) on the next slip.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate
from hrms.payroll.doctype.salary_slip.salary_slip import SalarySlip

from employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan import LIVE_STATUSES


class LoanSalarySlip(SalarySlip):
    def set_net_pay(self):
        set_loan_recoveries(self)
        super().set_net_pay()

    def on_submit(self):
        super().on_submit()
        post_loan_recoveries(self)

    def on_cancel(self):
        super().on_cancel()
        reverse_loan_recoveries(self)


# ----------------------------------------------------------------------
# Draft / submit: work out what this slip recovers
# ----------------------------------------------------------------------


def live_loans(slip, for_update=False):
    names = frappe.get_all(
        "Loan",
        filters={
            "employee": slip.employee,
            "company": slip.company,
            "docstatus": 1,
            "status": ["in", LIVE_STATUSES],
        },
        order_by="creation asc, name asc",
        pluck="name",
    )
    loans = [frappe.get_doc("Loan", n) for n in names]
    if for_update:
        for loan in sorted(loans, key=lambda d: d.name):  # fixed order: no lock-order deadlocks
            loan.lock()
    return loans


def set_loan_recoveries(slip):
    """Rebuild ``loan_recoveries`` from the loans' current state.

    Runs on every validate. At submit (docstatus 1) the loans are locked
    first, so the figures that get posted are computed against rows no
    other transaction can be recovering at the same time. A re-run of
    payroll for a period that is already recovered finds nothing due.
    """
    slip.set("loan_recoveries", [])
    slip.total_loan_repayment = 0

    loans = live_loans(slip, for_update=slip.docstatus == 1)
    if not loans:
        return

    precision = slip.precision("total_loan_repayment")
    capacity = flt(flt(slip.gross_pay) - flt(slip.get_component_totals("deductions")), precision)

    for loan in loans:
        if capacity <= 0:
            break
        allocations, unallocated = loan.allocate(capacity, slip.end_date, current_from=slip.start_date)
        for a in allocations:
            row = a["row"]
            total = flt(a["principal"] + a["interest"], precision)
            slip.append(
                "loan_recoveries",
                {
                    "loan": loan.name,
                    "schedule_period": row.period_no,
                    "principal_amount": a["principal"],
                    "interest_amount": a["interest"],
                    "arrears_amount": a["principal"] if a["is_arrears"] else 0,
                    "total_recovered": total,
                },
            )
            slip.total_loan_repayment = flt(slip.total_loan_repayment + total, precision)
        capacity = unallocated


# ----------------------------------------------------------------------
# On submit: move the loans and post the ledger
# ----------------------------------------------------------------------


def post_loan_recoveries(slip):
    if not slip.get("loan_recoveries"):
        return

    loans = {}
    for row in slip.loan_recoveries:
        loan = loans.get(row.loan) or frappe.get_doc("Loan", row.loan).lock()
        loans[row.loan] = loan
        loan.apply_recovery(row.schedule_period, row.principal_amount, row.interest_amount, as_of=slip.end_date)

    je = make_recovery_journal_entry(slip, loans)
    slip.db_set("loan_journal_entry", je.name)


def payroll_payable_account(slip):
    account = None
    if slip.get("payroll_entry"):
        account = frappe.db.get_value("Payroll Entry", slip.payroll_entry, "payroll_payable_account")
    account = account or frappe.get_cached_value("Company", slip.company, "default_payroll_payable_account")
    if not account:
        frappe.throw(
            _("Set a Default Payroll Payable Account on Company {0} to post loan recoveries.").format(slip.company)
        )
    return account


def party_fields(account, employee):
    if frappe.get_cached_value("Account", account, "account_type") in ("Payable", "Receivable"):
        return {"party_type": "Employee", "party": employee}
    return {}


def make_recovery_journal_entry(slip, loans):
    """Dr Salary Payable (total) / Cr Loan Receivable (principal)
    / Cr Interest Income (interest), one pair of credit lines per loan.

    Interest is recognised here, when it is recovered (DECISIONS.md §4), so
    the receivable only ever carries principal and ties to the sum of
    outstanding principal."""
    payable = payroll_payable_account(slip)
    cost_center = frappe.get_cached_value("Company", slip.company, "cost_center")
    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = slip.company
    je.posting_date = slip.posting_date
    je.user_remark = _("Loan recovery through Salary Slip {0} ({1} to {2})").format(
        slip.name, slip.start_date, slip.end_date
    )

    total = 0
    for name, loan in loans.items():
        rows = [r for r in slip.loan_recoveries if r.loan == name]
        principal = sum(flt(r.principal_amount) for r in rows)
        interest = sum(flt(r.interest_amount) for r in rows)
        total += principal + interest
        if principal:
            je.append(
                "accounts",
                {
                    "account": loan.loan_account,
                    "credit_in_account_currency": principal,
                    "cost_center": cost_center,
                    "user_remark": loan.name,
                    **party_fields(loan.loan_account, slip.employee),
                },
            )
        if interest:
            je.append(
                "accounts",
                {
                    "account": loan.interest_account,
                    "credit_in_account_currency": interest,
                    "cost_center": cost_center,
                    "user_remark": loan.name,
                },
            )

    je.append(
        "accounts",
        {
            "account": payable,
            "debit_in_account_currency": total,
            "cost_center": cost_center,
            **party_fields(payable, slip.employee),
        },
    )
    je.flags.ignore_permissions = True
    je.insert()
    je.submit()
    return je


# ----------------------------------------------------------------------
# On cancel: undo exactly what this slip recorded
# ----------------------------------------------------------------------


def reverse_loan_recoveries(slip):
    rows = list(slip.get("loan_recoveries") or [])
    if not rows:
        return

    loans = {}
    for name in sorted({r.loan for r in rows}):
        loans[name] = frappe.get_doc("Loan", name).lock()

    for row in sorted(rows, key=lambda r: r.idx, reverse=True):
        loan = loans[row.loan]
        loan.reverse_recovery(row.schedule_period, row.principal_amount, row.interest_amount, as_of=slip.end_date)

    for name, loan in loans.items():
        periods = sorted({r.schedule_period for r in rows if r.loan == name})
        later = [
            r.period_no
            for r in loan.repayment_schedule
            if r.period_no > periods[-1] and flt(r.recovered_amount) > 0
        ]
        note = _("Salary Slip {0} cancelled: recovery of period(s) {1} reversed.").format(
            slip.name, ", ".join(map(str, periods))
        )
        if later:
            # Retrospective correction (DECISIONS.md §8): later periods keep
            # their recoveries; the reversed period is due again and the next
            # submitted slip (the amendment, or the current month) takes it.
            note += " " + _(
                "Later period(s) {0} were already recovered and are left as posted; the reversed amount is now arrears."
            ).format(", ".join(map(str, later)))
        loan.add_comment("Info", note)

    if slip.get("loan_journal_entry"):
        je = frappe.get_doc("Journal Entry", slip.loan_journal_entry)
        if je.docstatus == 1:
            je.flags.ignore_permissions = True
            je.cancel()
