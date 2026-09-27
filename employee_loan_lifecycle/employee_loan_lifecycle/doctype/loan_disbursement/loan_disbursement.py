
# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime


class LoanDisbursement(Document):
    def validate(self):
        self.validate_required_fields()
        self.validate_amount()
        self.validate_loan()
        self.validate_workflow()

    def validate_required_fields(self):
        required_fields = [
            "loan",
            "employee",
            "company",
            "disbursement_date",
            "amount",
        ]

        for field in required_fields:
            if not self.get(field):
                frappe.throw(_("{0} is required").format(
                    self.meta.get_label(field)
                ))

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

        sanctioned_amount = flt(
            loan.get("loan_amount") or loan.get("sanctioned_amount")
        )

        if sanctioned_amount <= 0:
            frappe.throw(_("The Loan has no valid sanctioned amount."))

        # Count all other disbursements that have reached release.
        other_disbursed = frappe.db.sql(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM `tabLoan Disbursement`
            WHERE loan = %s
              AND name != %s
              AND status IN (
                  'Treasury Released',
                  'Disbursed'
              )
            """,
            (self.loan, self.name or ""),
        )[0][0]

        current_amount = flt(self.amount)
        total = flt(other_disbursed) + current_amount

        if self.status in ("Treasury Released", "Disbursed"):
            if total > sanctioned_amount:
                frappe.throw(
                    _(
                        "Total disbursements ({0}) cannot exceed "
                        "the sanctioned amount ({1})."
                    ).format(total, sanctioned_amount)
                )

        elif self.status not in ("Cancelled",):
            # Reserve this draft amount too, so concurrent drafts do not
            # silently appear to fit when submitted for release.
            other_reserved = frappe.db.sql(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM `tabLoan Disbursement`
                WHERE loan = %s
                  AND name != %s
                  AND status IN (
                      'Draft',
                      'Finance Verified'
                  )
                """,
                (self.loan, self.name or ""),
            )[0][0]

            total_reserved = (
                flt(other_disbursed)
                + flt(other_reserved)
                + current_amount
            )

            if total_reserved > sanctioned_amount:
                frappe.throw(
                    _(
                        "Total released and reserved disbursements "
                        "({0}) cannot exceed the sanctioned amount ({1})."
                    ).format(total_reserved, sanctioned_amount)
                )

    def validate_workflow(self):
        allowed_statuses = [
            "Draft",
            "Finance Verified",
            "Treasury Released",
            "Disbursed",
            "Cancelled",
        ]

        if self.status and self.status not in allowed_statuses:
            frappe.throw(_("Invalid disbursement status."))

        if self.status in ("Treasury Released", "Disbursed"):
            if not self.finance_verifier:
                frappe.throw(_("Finance verification is required."))

            if not self.treasury_releaser:
                frappe.throw(_("Treasury release is required."))

            if self.finance_verifier == self.treasury_releaser:
                frappe.throw(
                    _("The verifier and treasury releaser must be different users.")
                )

        if self.status == "Finance Verified" and not self.finance_verifier:
            frappe.throw(_("Finance verifier is required."))

    def before_save(self):
        if self.is_new() and not self.status:
            self.status = "Draft"

    @frappe.whitelist()
    def finance_verify(self):
        self.check_permission("write")

        if self.status != "Draft":
            frappe.throw(
                _("Only Draft disbursements can be finance verified.")
            )

        self.check_role("Finance Manager")

        user = frappe.session.user
        if user == self.get("treasury_releaser"):
            frappe.throw(
                _("The treasury releaser cannot verify this disbursement.")
            )

        self.finance_verifier = user
        self.verification_date = now_datetime()
        self.status = "Finance Verified"
        self.save()
        return self.status

    @frappe.whitelist()
    def treasury_release(self):
        self.check_permission("write")

        if self.status != "Finance Verified":
            frappe.throw(
                _("Only Finance Verified disbursements can be released.")
            )

        self.check_role("Treasury Manager")

        user = frappe.session.user
        if user == self.get("finance_verifier"):
            frappe.throw(
                _("The finance verifier cannot release this disbursement.")
            )

        self.treasury_releaser = user
        self.release_date = now_datetime()
        self.status = "Treasury Released"
        self.save()

        # Accounting/payment integration should be implemented here:
        # 1. Create the payment document against employee bank details.
        # 2. Post Dr Employee Loan Receivable / Cr Bank.
        # 3. Store the payment document reference.
        #
        # Do not mark as Disbursed until the payment and ledger posting
        # have succeeded.

        return self.status

    @frappe.whitelist()
    def mark_disbursed(self):
        self.check_permission("write")

        if self.status != "Treasury Released":
            frappe.throw(
                _("Only Treasury Released disbursements can be completed.")
            )

        self.check_role("Treasury Manager")

        if frappe.session.user != self.treasury_releaser:
            frappe.throw(
                _("Only the treasury releaser can complete this disbursement.")
            )

        if not self.get("payment_entry"):
            frappe.throw(
                _("Link the successful payment document before marking Disbursed.")
            )

        self.status = "Disbursed"
        self.save()
        return self.status

    @frappe.whitelist()
    def cancel_disbursement(self, reason):
        self.check_permission("write")

        if self.status not in (
            "Finance Verified",
            "Treasury Released",
            "Disbursed",
        ):
            frappe.throw(
                _("This disbursement cannot be cancelled from its current status.")
            )

        if not reason or not reason.strip():
            frappe.throw(_("Please provide a cancellation reason."))

        self.check_role("System Manager")

        if self.status in ("Treasury Released", "Disbursed"):
            if not self.get("reversal_entry"):
                frappe.throw(
                    _(
                        "Create and link the accounting reversal before "
                        "cancelling a released disbursement."
                    )
                )

        self.status = "Cancelled"
        self.add_comment(
            "Comment",
            _("Disbursement cancelled: {0}").format(reason.strip()),
        )
        self.save()
        return self.status

    def check_role(self, role):
        if role not in frappe.get_roles():
            frappe.throw(
                _("You need the {0} role to perform this action.").format(role),
                frappe.PermissionError,
            )