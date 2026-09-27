# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt

"""Allocation: interest on due periods -> current principal -> arrears.
Leftover = prepayment (Lump Sum / Foreclosure only).
Ledger: PE Dr Bank / Cr Receivable (full); JE Dr Receivable / Cr Interest Income (interest + charge)."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_first_day, getdate, nowdate


class LoanRepayment(Document):
    def validate(self):
        loan = frappe.get_doc("Loan", self.loan)
        if loan.docstatus != 1:
            frappe.throw(_("Only a sanctioned (submitted) Loan can be repaid."))
        if loan.status in ("Closed", "Written Off", "Sanctioned"):
            frappe.throw(_("Loan {0} is {1}; there is nothing to repay.").format(loan.name, loan.status))
        self.employee = loan.employee
        self.company = loan.company
        if getdate(self.repayment_date) > getdate(nowdate()):
            frappe.throw(_("Repayment date cannot be in the future."))
        if flt(self.amount) <= 0:
            frappe.throw(_("Repayment amount must be greater than zero."))
        if self.repayment_type == "Foreclosure":
            self.set_foreclosure_amount(loan)
        self.allocate(loan)  # preview; recomputed under lock at submit

    def set_foreclosure_amount(self, loan):
        p = loan.precision_digits
        allocations, _rest = loan.allocate(10**12, self.repayment_date, get_first_day(self.repayment_date))
        dues = sum(a["interest"] + a["principal"] for a in allocations)
        future = loan.future_principal(self.repayment_date)
        rate = flt(frappe.db.get_value("Loan Type", loan.loan_type, "pre_closure_charge_percent"))
        self.pre_closure_charge = flt(future * rate / 100, p)
        payoff = flt(dues + future + self.pre_closure_charge, p)
        if abs(flt(self.amount) - payoff) > 0.001:
            frappe.msgprint(
                _("Foreclosure amount set to the payoff figure {0} (dues {1} + principal {2} + charge {3}).").format(
                    payoff, flt(dues, p), future, self.pre_closure_charge
                ),
                alert=True,
            )
        self.amount = payoff

    def allocate(self, loan):
        p = loan.precision_digits
        charge = flt(self.pre_closure_charge) if self.repayment_type == "Foreclosure" else 0
        allocations, remaining = loan.allocate(
            flt(self.amount) - charge, self.repayment_date, get_first_day(self.repayment_date)
        )

        self.set("recovery_breakdown", [])
        for a in allocations:
            self.append(
                "recovery_breakdown",
                {
                    "loan": loan.name,
                    "schedule_period": a["row"].period_no,
                    "interest_amount": a["interest"],
                    "principal_amount": a["principal"],
                    "arrears_amount": a["principal"] if a["is_arrears"] else 0,
                    "total_recovered": flt(a["interest"] + a["principal"], p),
                },
            )

        self.prepayment = 0
        if remaining > 0.001:
            if self.repayment_type not in ("Lump Sum", "Foreclosure"):
                frappe.throw(
                    _(
                        "{0} is more than is currently due ({1} left over). Use Lump Sum to prepay principal, "
                        "or Foreclosure to close the loan."
                    ).format(flt(self.amount), remaining)
                )
            future = loan.future_principal(self.repayment_date)
            if remaining - future > 0.001:
                frappe.throw(
                    _("{0} exceeds everything owed on the loan by {1}.").format(flt(self.amount), flt(remaining - future, p))
                )
            self.prepayment = remaining

        rows = self.recovery_breakdown
        self.allocated_interest = flt(sum(flt(r.interest_amount) for r in rows), p)
        self.allocated_arrears = flt(sum(flt(r.arrears_amount) for r in rows), p)
        self.allocated_principal = flt(
            sum(flt(r.principal_amount) for r in rows) - self.allocated_arrears + self.prepayment, p
        )

    def on_submit(self):
        loan = frappe.get_doc("Loan", self.loan).lock()
        self.allocate(loan)
        for row in self.recovery_breakdown:
            loan.apply_recovery(row.schedule_period, row.principal_amount, row.interest_amount, as_of=self.repayment_date)

        if self.prepayment:
            period = loan.apply_prepayment(self.prepayment, self.repayment_date)
            self.append(
                "recovery_breakdown",
                {
                    "loan": loan.name,
                    "schedule_period": period,
                    "principal_amount": self.prepayment,
                    "interest_amount": 0,
                    "arrears_amount": 0,
                    "total_recovered": self.prepayment,
                },
            )

        frappe.db.delete("Loan Recovery Detail", {"parent": self.name, "parenttype": "Loan Repayment"})
        for i, row in enumerate(self.recovery_breakdown, start=1):
            row.idx = i
            row.db_insert()
        self.db_update()

        pe = self.make_payment_entry(loan)
        self.db_set("payment_entry", pe.name)
        income = flt(self.allocated_interest) + flt(self.pre_closure_charge)
        if income:
            je = self.make_interest_journal_entry(loan, income)
            self.db_set("interest_journal_entry", je.name)

    def before_cancel(self):
        self.ignore_linked_doctypes = ("Payment Entry", "Journal Entry", "GL Entry", "Payment Ledger Entry")

    def on_cancel(self):
        loan = frappe.get_doc("Loan", self.loan).lock()
        for row in sorted(self.recovery_breakdown, key=lambda r: r.idx, reverse=True):
            if loan.get_row(row.schedule_period).row_type == "Prepayment":
                loan.reverse_prepayment(row.schedule_period)
            else:
                loan.reverse_recovery(row.schedule_period, row.principal_amount, row.interest_amount)

        for doctype, name in (("Journal Entry", self.interest_journal_entry), ("Payment Entry", self.payment_entry)):
            if name:
                doc = frappe.get_doc(doctype, name)
                if doc.docstatus == 1:
                    doc.flags.ignore_permissions = True
                    doc.cancel()

    def make_payment_entry(self, loan):
        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Receive"
        pe.company = loan.company
        pe.posting_date = self.repayment_date
        pe.mode_of_payment = self.mode_of_payment
        pe.party_type = "Employee"
        pe.party = loan.employee
        pe.party_account = loan.loan_account
        pe.paid_from = loan.loan_account
        pe.paid_to = self.get_bank_gl_account(loan.company)
        pe.paid_amount = flt(self.amount)
        pe.received_amount = flt(self.amount)
        pe.reference_no = self.reference_no or self.name
        pe.reference_date = self.repayment_date
        pe.remarks = _("Loan {0} {1} repayment ({2})").format(loan.name, self.repayment_type, self.name)
        pe.flags.ignore_permissions = True
        pe.insert()
        pe.submit()
        return pe

    def make_interest_journal_entry(self, loan, income):
        cost_center = frappe.get_cached_value("Company", loan.company, "cost_center")
        je = frappe.new_doc("Journal Entry")
        je.company = loan.company
        je.posting_date = self.repayment_date
        je.user_remark = _("Interest recognised on Loan Repayment {0} ({1})").format(self.name, loan.name)
        je.append("accounts", {
            "account": loan.loan_account, "debit_in_account_currency": income,
            "party_type": "Employee", "party": loan.employee, "cost_center": cost_center,
        })
        je.append("accounts", {
            "account": loan.interest_account, "credit_in_account_currency": income, "cost_center": cost_center,
        })
        je.flags.ignore_permissions = True
        je.insert()
        je.submit()
        return je

    def get_bank_gl_account(self, company):
        account = None
        if self.mode_of_payment:
            account = frappe.db.get_value(
                "Mode of Payment Account", {"parent": self.mode_of_payment, "company": company}, "default_account"
            )
        account = account or frappe.get_cached_value("Company", company, "default_bank_account")
        if not account:
            frappe.throw(_("Set a default account for Mode of Payment {0} in {1}.").format(self.mode_of_payment, company))
        return account


def get_permission_query_conditions(user=None):
    from employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan import get_permission_query_conditions as loan_pqc
    return loan_pqc(user, doctype="Loan Repayment")


def has_permission(doc, ptype="read", user=None, debug=False):
    from employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan import has_permission as loan_perm
    return loan_perm(doc, ptype, user)