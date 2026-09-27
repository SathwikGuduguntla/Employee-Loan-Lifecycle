# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime, nowdate


class LoanDisbursement(Document):
    def validate(self):
        self.validate_required_fields()
        self.validate_amount()
        self.validate_loan()
        self.validate_workflow()

    def validate_required_fields(self):
        required_fields = ["loan", "employee", "company", "disbursement_date", "amount"]

        for field in required_fields:
            if not self.get(field):
                frappe.throw(_("{0} is required").format(self.meta.get_label(field)))

    def validate_amount(self):
        if flt(self.amount) <= 0:
            frappe.throw(_("Disbursement amount must be greater than zero."))

    def validate_loan(self):
        loan = frappe.get_doc("Loan", self.loan)

        if loan.docstatus != 1:
            frappe.throw(_("Only submitted Loans can be disbursed."))

        if loan.employee != self.employee:
            frappe.throw(_("Employee does not match the selected Loan."))

        if loan.company != self.company:
            frappe.throw(_("Company does not match the selected Loan."))

        sanctioned_amount = flt(loan.sanctioned_amount)
        if sanctioned_amount <= 0:
            frappe.throw(_("The Loan has no valid sanctioned amount."))

        other_disbursed = flt(
            frappe.db.sql(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM `tabLoan Disbursement`
                WHERE loan = %s AND name != %s
                  AND status IN ('Treasury Released', 'Disbursed')
                """,
                (self.loan, self.name or ""),
            )[0][0]
        )

        current_amount = flt(self.amount)
        total = other_disbursed + current_amount

        if self.status in ("Treasury Released", "Disbursed"):
            if total > sanctioned_amount:
                frappe.throw(
                    _(
                        "Total disbursements ({0}) cannot exceed the sanctioned amount ({1})."
                    ).format(total, sanctioned_amount)
                )
        elif self.status != "Cancelled":
            # Reserve this draft amount too, so concurrent drafts do not
            # silently appear to fit when submitted for release.
            other_reserved = flt(
                frappe.db.sql(
                    """
                    SELECT COALESCE(SUM(amount), 0)
                    FROM `tabLoan Disbursement`
                    WHERE loan = %s AND name != %s
                      AND status IN ('Draft', 'Finance Verified')
                    """,
                    (self.loan, self.name or ""),
                )[0][0]
            )

            total_reserved = other_disbursed + other_reserved + current_amount
            if total_reserved > sanctioned_amount:
                frappe.throw(
                    _(
                        "Total released and reserved disbursements ({0}) cannot "
                        "exceed the sanctioned amount ({1})."
                    ).format(total_reserved, sanctioned_amount)
                )

    def validate_workflow(self):
        allowed_statuses = ["Draft", "Finance Verified", "Treasury Released", "Disbursed", "Cancelled"]

        if self.status and self.status not in allowed_statuses:
            frappe.throw(_("Invalid disbursement status."))

        if self.status in ("Treasury Released", "Disbursed"):
            if not self.verified_by:
                frappe.throw(_("Finance verification is required."))
            if not self.released_by:
                frappe.throw(_("Treasury release is required."))
            if self.verified_by == self.released_by:
                frappe.throw(_("The verifier and treasury releaser must be different users."))

        if self.status == "Finance Verified" and not self.verified_by:
            frappe.throw(_("Finance verifier is required."))

    def before_save(self):
        if self.is_new() and not self.status:
            self.status = "Draft"

    # ------------------------------------------------------------------
    # Segregation-of-duties workflow
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def finance_verify(self):
        self.check_permission("write")

        if self.status != "Draft":
            frappe.throw(_("Only Draft disbursements can be finance verified."))

        self.check_role("Finance Manager")

        user = frappe.session.user
        if user == self.get("released_by"):
            frappe.throw(_("The treasury releaser cannot verify this disbursement."))

        self.verified_by = user
        self.verified_on = now_datetime()
        self.status = "Finance Verified"
        self.save()
        return self.status

    @frappe.whitelist()
    def treasury_release(self):
        self.check_permission("write")

        if self.status != "Finance Verified":
            frappe.throw(_("Only Finance Verified disbursements can be released."))

        self.check_role("Treasury Manager")

        user = frappe.session.user
        if user == self.get("verified_by"):
            frappe.throw(_("The finance verifier cannot release this disbursement."))

        self.released_by = user
        self.released_on = now_datetime()
        self.status = "Treasury Released"

        if not self.payment_entry:
            payment_entry = self.make_payment_entry()
            self.payment_entry = payment_entry.name

        self.save()
        return self.status

    @frappe.whitelist()
    def mark_disbursed(self):
        self.check_permission("write")

        if self.status != "Treasury Released":
            frappe.throw(_("Only Treasury Released disbursements can be completed."))

        self.check_role("Treasury Manager")

        if frappe.session.user != self.released_by:
            frappe.throw(_("Only the treasury releaser can complete this disbursement."))

        if not self.payment_entry:
            frappe.throw(_("No payment document is linked to this disbursement."))

        pe_status = frappe.db.get_value("Payment Entry", self.payment_entry, "docstatus")
        if pe_status != 1:
            frappe.throw(_("The linked Payment Entry must be submitted before marking Disbursed."))

        self.status = "Disbursed"
        self.save()

        loan = frappe.get_doc("Loan", self.loan)
        loan.apply_disbursement(self.amount)

        return self.status

    @frappe.whitelist()
    def cancel_disbursement(self, reason):
        self.check_permission("write")

        if self.status not in ("Finance Verified", "Treasury Released", "Disbursed"):
            frappe.throw(_("This disbursement cannot be cancelled from its current status."))

        if not reason or not reason.strip():
            frappe.throw(_("Please provide a cancellation reason."))

        self.check_role("System Manager")

        was_disbursed = self.status == "Disbursed"

        if self.payment_entry:
            pe = frappe.get_doc("Payment Entry", self.payment_entry)
            if pe.docstatus == 1:
                # Cancelling the Payment Entry lets ERPNext's own accounting
                # engine post the reversing GL entries; there is no separate
                # reversal_entry field on this doctype to track by hand.
                pe.cancel()

        self.status = "Cancelled"
        self.add_comment("Comment", _("Disbursement cancelled: {0}").format(reason.strip()))
        self.save()

        if was_disbursed:
            loan = frappe.get_doc("Loan", self.loan)
            loan.reverse_disbursement(self.amount)

        return self.status

    def check_role(self, role):
        if role not in frappe.get_roles():
            frappe.throw(
                _("You need the {0} role to perform this action.").format(role),
                frappe.PermissionError,
            )

    # ------------------------------------------------------------------
    # Accounting — Dr Employee Loan Receivable / Cr Bank, via a Payment
    # Entry so ERPNext's own ledger posting is reused rather than
    # hand-rolled GL Entry rows.
    # ------------------------------------------------------------------

    def make_payment_entry(self):
        loan = frappe.get_doc("Loan", self.loan)
        bank_account = self.get_bank_cash_account()

        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Pay"
        pe.company = self.company
        pe.posting_date = self.disbursement_date or nowdate()
        pe.party_type = "Employee"
        pe.party = self.employee
        pe.paid_from = bank_account
        pe.paid_to = loan.loan_account
        pe.paid_amount = flt(self.amount)
        pe.received_amount = flt(self.amount)
        pe.mode_of_payment = self.mode_of_payment
        pe.reference_no = self.reference_no or self.name
        pe.reference_date = self.reference_date or self.disbursement_date or nowdate()
        pe.remarks = _("Loan disbursement against {0} for {1}").format(self.loan, self.employee)
        pe.insert(ignore_permissions=True)
        pe.submit()
        return pe

    def get_bank_cash_account(self):
        account = None

        if self.bank_account:
            account = frappe.db.get_value("Bank Account", self.bank_account, "account")

        if not account and self.mode_of_payment:
            account = frappe.db.get_value(
                "Mode of Payment Account",
                {"parent": self.mode_of_payment, "company": self.company},
                "default_account",
            )

        if not account:
            account = frappe.get_cached_value("Company", self.company, "default_bank_account") or (
                frappe.get_cached_value("Company", self.company, "default_cash_account")
            )

        if not account:
            frappe.throw(
                _(
                    "Could not resolve a bank/cash account for this disbursement. Set a "
                    "default account against the Bank Account or Mode of Payment for {0}."
                ).format(self.company)
            )

        return account


# ----------------------------------------------------------------------
# Row-level access control. Wired via hooks.py for "Loan Disbursement".
# ----------------------------------------------------------------------


def get_permission_query_conditions(user=None):
    user = user or frappe.session.user

    if any(
        role in frappe.get_roles(user)
        for role in ("System Manager", "Finance Manager", "Treasury Manager")
    ):
        return ""

    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    if not employee:
        return "1=0"

    return f"`tabLoan Disbursement`.employee = {frappe.db.escape(employee)}"


def has_permission(doc, ptype="read", user=None):
    user = user or frappe.session.user

    if any(
        role in frappe.get_roles(user)
        for role in ("System Manager", "Finance Manager", "Treasury Manager")
    ):
        return True

    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    return bool(employee) and doc.employee == employee
