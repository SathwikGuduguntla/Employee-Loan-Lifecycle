# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, nowdate


class LoanRepayment(Document):
    def validate(self):
        self.validate_required_fields()
        self.validate_loan()

        if self.repayment_type == "Foreclosure":
            self.compute_foreclosure_amount()

        if flt(self.amount) <= 0:
            frappe.throw(_("Repayment amount must be greater than zero."))

    def validate_required_fields(self):
        for field in ("loan", "repayment_date", "repayment_type", "amount"):
            if not self.get(field):
                frappe.throw(_("{0} is required.").format(self.meta.get_label(field)))

        if getdate(self.repayment_date) > getdate(nowdate()):
            frappe.throw(_("Repayment Date cannot be in the future."))

    def validate_loan(self):
        loan = frappe.get_doc("Loan", self.loan)

        if loan.docstatus != 1:
            frappe.throw(_("Only submitted Loans can receive repayments."))

        if loan.status in ("Closed", "Written Off"):
            frappe.throw(_("This Loan is already {0}.").format(loan.status))

        if getdate(self.repayment_date) < getdate(loan.creation):
            frappe.throw(_("Repayment Date cannot be before the Loan was sanctioned."))

    def compute_foreclosure_amount(self):
        loan = frappe.get_doc("Loan", self.loan)
        loan_type = frappe.get_doc("Loan Type", loan.loan_type)

        charge = round(
            flt(loan.outstanding_principal) * flt(loan_type.pre_closure_charge_percent) / 100, 2
        )
        required = round(
            flt(loan.outstanding_principal) + flt(loan.outstanding_interest) + charge, 2
        )

        if flt(self.amount) < required - 0.01:
            frappe.throw(
                _(
                    "Foreclosure requires the full payoff amount of {0} "
                    "(outstanding principal {1} + outstanding interest {2} "
                    "+ pre-closure charge {3})."
                ).format(
                    required, loan.outstanding_principal, loan.outstanding_interest, charge
                )
            )

        # A foreclosure is a full payoff, not a partial one — cap it to the
        # exact figure rather than letting an accidental overpayment through.
        self.amount = required
        self._foreclosure_charge = charge

    def before_submit(self):
        self.allocate_and_apply()

        if not self.payment_entry:
            payment_entry = self.make_payment_entry()
            self.payment_entry = payment_entry.name

    def on_cancel(self):
        if not self.recovery_breakdown:
            return

        loan = frappe.get_doc("Loan", self.loan)
        # Reverse in the opposite order they were applied.
        for row in sorted(self.recovery_breakdown, key=lambda r: r.idx, reverse=True):
            loan.reverse_recovery(row.schedule_period, row.principal_amount, row.interest_amount)
            loan.reload()

        if self.payment_entry:
            pe = frappe.get_doc("Payment Entry", self.payment_entry)
            if pe.docstatus == 1:
                pe.cancel()

    # ------------------------------------------------------------------
    # Allocation engine
    # ------------------------------------------------------------------

    def allocate_and_apply(self):
        loan = frappe.get_doc("Loan", self.loan)
        as_on = getdate(self.repayment_date)
        remaining = flt(self.amount)

        total_interest = 0.0
        total_principal = 0.0
        total_arrears = 0.0

        self.set("recovery_breakdown", [])

        rows = sorted(loan.repayment_schedule, key=lambda r: r.period_no)

        for row in rows:
            if remaining <= 0.01:
                break

            instalment = flt(row.instalment_amount)
            recovered = flt(row.recovered_amount)
            due = round(instalment - recovered, 2)
            if due <= 0.01:
                continue

            # Interest-first convention applied to what's already recovered
            # on this row, so the remaining split is reproducible without a
            # separate interest_recovered/principal_recovered field.
            interest_recovered = min(recovered, flt(row.interest_amount))
            principal_recovered = recovered - interest_recovered
            interest_due = round(flt(row.interest_amount) - interest_recovered, 2)
            principal_due = round(flt(row.principal_amount) - principal_recovered, 2)

            pay_interest = min(remaining, max(interest_due, 0))
            remaining = round(remaining - pay_interest, 2)

            pay_principal = min(remaining, max(principal_due, 0))
            remaining = round(remaining - pay_principal, 2)

            if pay_interest <= 0 and pay_principal <= 0:
                continue

            is_arrears = getdate(row.due_date) < as_on

            loan.apply_recovery(row.period_no, pay_principal, pay_interest)
            loan.reload()

            if is_arrears:
                total_arrears += pay_interest + pay_principal
            else:
                total_interest += pay_interest
                total_principal += pay_principal

            self.append(
                "recovery_breakdown",
                {
                    "loan": self.loan,
                    "schedule_period": row.period_no,
                    "principal_amount": pay_principal,
                    "interest_amount": pay_interest,
                    "arrears_amount": (pay_interest + pay_principal) if is_arrears else 0,
                    "total_recovered": pay_interest + pay_principal,
                },
            )

        if remaining > 0.01:
            frappe.throw(
                _(
                    "This repayment exceeds the Loan's total outstanding by {0}. "
                    "Reduce the amount or use Foreclosure for a full payoff."
                ).format(remaining)
            )

        self.allocated_interest = round(total_interest, 2)
        self.allocated_principal = round(total_principal, 2)
        self.allocated_arrears = round(total_arrears, 2)

    # ------------------------------------------------------------------
    # Accounting — Dr Bank / Cr Employee Loan Receivable.
    # ------------------------------------------------------------------

    def make_payment_entry(self):
        loan = frappe.get_doc("Loan", self.loan)
        bank_account = self.get_bank_cash_account(loan.company)

        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Receive"
        pe.company = loan.company
        pe.posting_date = self.repayment_date or nowdate()
        pe.party_type = "Employee"
        pe.party = self.employee or loan.employee
        pe.paid_from = loan.loan_account
        pe.paid_to = bank_account
        pe.paid_amount = flt(self.amount)
        pe.received_amount = flt(self.amount)
        pe.mode_of_payment = self.mode_of_payment
        pe.reference_no = self.reference_no or self.name
        pe.reference_date = self.repayment_date or nowdate()
        pe.remarks = _("Loan repayment ({0}) against {1}").format(self.repayment_type, self.loan)
        pe.insert(ignore_permissions=True)
        pe.submit()
        return pe

    def get_bank_cash_account(self, company):
        account = None

        if self.mode_of_payment:
            account = frappe.db.get_value(
                "Mode of Payment Account",
                {"parent": self.mode_of_payment, "company": company},
                "default_account",
            )

        if not account:
            account = frappe.get_cached_value(
                "Company", company, "default_bank_account"
            ) or frappe.get_cached_value("Company", company, "default_cash_account")

        if not account:
            frappe.throw(
                _(
                    "Could not resolve a bank/cash account for this repayment. Set a "
                    "default account against the Mode of Payment for {0}."
                ).format(company)
            )

        return account


# ----------------------------------------------------------------------
# Row-level access control. Wired via hooks.py for "Loan Repayment".
# ----------------------------------------------------------------------


def get_permission_query_conditions(user=None):
    user = user or frappe.session.user

    if any(
        role in frappe.get_roles(user)
        for role in ("System Manager", "Finance Manager", "HR Manager")
    ):
        return ""

    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    if not employee:
        return "1=0"

    return f"`tabLoan Repayment`.employee = {frappe.db.escape(employee)}"


def has_permission(doc, ptype="read", user=None):
    user = user or frappe.session.user

    if any(
        role in frappe.get_roles(user)
        for role in ("System Manager", "Finance Manager", "HR Manager")
    ):
        return True

    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    return bool(employee) and doc.employee == employee
