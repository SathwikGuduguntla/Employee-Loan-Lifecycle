# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt

"""Loan Disbursement: one tranche of a sanctioned loan.

Flow (docstatus in brackets):

    Draft (0) --finance_verify()--> Finance Verified (0)
              --treasury_release()--> Treasury Released (1)   money leaves, ledger posts
              --mark_disbursed()--> Disbursed (1)             bank confirms credit

Release *is* submit. That is deliberate: the moment the ledger moves is the
moment the document becomes immutable, and Frappe's own cancel/amend then
gives the reversal path for free. The standard Submit button is refused
unless it comes through treasury_release(), so the segregation-of-duties
checks cannot be skipped by using the stock button or a raw API submit.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

FINANCE = "Finance Manager"
TREASURY = "Treasury Officer"
# Set only by the transition methods below. A client can post any JSON it
# likes to /api/resource or run_doc_method, so these are re-checked on save.
SYSTEM_FIELDS = ("status", "verified_by", "verified_on", "released_by", "released_on", "tranche_no", "payment_entry")


class LoanDisbursement(Document):
    def validate(self):
        if self.docstatus == 0 and not self.status:
            self.status = "Draft"
        if flt(self.amount) <= 0:
            frappe.throw(_("Disbursement amount must be greater than zero."))

        loan = frappe.get_doc("Loan", self.loan)
        if loan.docstatus != 1:
            frappe.throw(_("Only a sanctioned (submitted) Loan can be disbursed."))
        if loan.status in ("Closed", "Written Off"):
            frappe.throw(_("Loan {0} is {1}.").format(loan.name, loan.status))
        self.employee = loan.employee
        self.company = loan.company

        self.guard_system_fields()
        self.validate_ceiling(loan)
        self.validate_segregation()

    def guard_system_fields(self):
        if self.flags.via_transition:
            return
        before = self.get_doc_before_save()
        for field in SYSTEM_FIELDS:
            old = before.get(field) if before else (self.meta.get_field(field).default or None)
            if (self.get(field) or None) != (old or None):
                frappe.throw(_("{0} is set by the system and cannot be edited.").format(self.meta.get_label(field)))

    def validate_ceiling(self, loan):
        """Released tranches + tranches in flight + this one <= sanctioned.

        Checked again under a row lock at submit, where it actually matters;
        this earlier check just stops Finance verifying a tranche that could
        never be released."""
        others = flt(
            frappe.db.sql(
                """select coalesce(sum(amount), 0) from `tabLoan Disbursement`
                where loan = %s and name != %s and docstatus < 2""",
                (self.loan, self.name or ""),
            )[0][0]
        )
        if others + flt(self.amount) > flt(loan.sanctioned_amount) + 0.001:
            frappe.throw(
                _("Released and pending tranches ({0}) plus this one ({1}) exceed the sanctioned amount ({2}).").format(
                    others, flt(self.amount), loan.sanctioned_amount
                )
            )

    def validate_segregation(self):
        if self.verified_by and self.released_by and self.verified_by == self.released_by:
            frappe.throw(_("The person who verifies a disbursement cannot also release it."))

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    @frappe.whitelist()
    def finance_verify(self):
        require_role(FINANCE)
        self.reload()  # act on the stored document, never on client-sent JSON
        self.check_permission("write")
        if self.docstatus != 0 or self.status != "Draft":
            frappe.throw(_("Only a Draft disbursement can be verified."))
        self.verified_by = frappe.session.user
        self.verified_on = now_datetime()
        self.status = "Finance Verified"
        self.flags.via_transition = True
        self.save()
        return self.status

    @frappe.whitelist()
    def treasury_release(self, reference_no=None, reference_date=None):
        require_role(TREASURY)
        self.reload()
        self.check_permission("submit")
        if self.docstatus != 0 or self.status != "Finance Verified":
            frappe.throw(_("Only a Finance Verified disbursement can be released."))
        if frappe.session.user == self.verified_by:
            frappe.throw(_("You verified this disbursement, so you cannot also release it."))
        if reference_no:
            self.reference_no = reference_no
        if reference_date:
            self.reference_date = reference_date
        self.released_by = frappe.session.user
        self.released_on = now_datetime()
        self.status = "Treasury Released"
        self.flags.via_treasury_release = True
        self.flags.via_transition = True
        self.submit()
        return self.status

    @frappe.whitelist()
    def mark_disbursed(self):
        require_role(TREASURY)
        self.reload()
        if self.docstatus != 1 or self.status != "Treasury Released":
            frappe.throw(_("Only a released disbursement can be marked as disbursed."))
        self.db_set("status", "Disbursed")
        self.add_comment("Info", _("Bank credit confirmed by {0}").format(frappe.session.user))
        return self.status

    # ------------------------------------------------------------------
    # Submit / cancel: the money
    # ------------------------------------------------------------------

    def before_submit(self):
        if not self.flags.via_treasury_release:
            frappe.throw(_("A disbursement is submitted by releasing it (Treasury Release), not directly."))
        if not (self.verified_by and self.released_by) or self.verified_by == self.released_by:
            frappe.throw(_("A disbursement needs a Finance verifier and a different Treasury releaser."))

    def on_submit(self):
        loan = frappe.get_doc("Loan", self.loan).lock()

        # Tranche numbers are assigned at release under the loan lock, so two
        # concurrent releases can neither share a number nor jointly overshoot
        # the sanctioned amount.
        self.tranche_no = (
            frappe.db.count("Loan Disbursement", {"loan": self.loan, "docstatus": 1, "name": ["!=", self.name]}) + 1
        )
        self.db_set("tranche_no", self.tranche_no)

        pe = self.make_payment_entry(loan)
        self.db_set("payment_entry", pe.name)
        loan.apply_disbursement(self.disbursement_date)

    def before_cancel(self):
        require_role(FINANCE)
        # The Payment Entry is ours; cancelling this document cancels it.
        self.ignore_linked_doctypes = ("Payment Entry", "GL Entry", "Payment Ledger Entry")

    def on_cancel(self):
        loan = frappe.get_doc("Loan", self.loan).lock()
        if self.payment_entry:
            pe = frappe.get_doc("Payment Entry", self.payment_entry)
            if pe.docstatus == 1:
                pe.flags.ignore_permissions = True
                pe.cancel()
        self.db_set("status", "Cancelled")
        loan.reverse_disbursement(self.disbursement_date)

    # ------------------------------------------------------------------
    # Ledger: Dr Employee Loan Receivable (party = employee) / Cr Bank
    # ------------------------------------------------------------------

    def make_payment_entry(self, loan):
        bank_gl = self.get_bank_gl_account()
        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Pay"
        pe.company = self.company
        pe.posting_date = getdate(self.disbursement_date)
        pe.mode_of_payment = self.mode_of_payment
        pe.party_type = "Employee"
        pe.party = self.employee
        # party_account must be set explicitly: left blank, ERPNext fills in
        # the employee's default *payable* account and paid_to is overwritten
        # with it, so the receivable would never be debited.
        pe.party_account = loan.loan_account
        pe.paid_to = loan.loan_account
        pe.paid_from = bank_gl
        pe.bank_account = self.bank_account
        pe.paid_amount = flt(self.amount)
        pe.received_amount = flt(self.amount)
        pe.reference_no = self.reference_no or self.name
        pe.reference_date = self.reference_date or self.disbursement_date
        pe.remarks = _("Loan {0} tranche {1} released to {2}. {3}").format(
            loan.name, self.tranche_no, self.employee, employee_bank_details(self.employee)
        )
        pe.flags.ignore_permissions = True
        pe.insert()
        pe.submit()
        return pe

    def get_bank_gl_account(self):
        account = frappe.db.get_value("Bank Account", self.bank_account, "account") if self.bank_account else None
        if not account and self.mode_of_payment:
            account = frappe.db.get_value(
                "Mode of Payment Account",
                {"parent": self.mode_of_payment, "company": self.company},
                "default_account",
            )
        if not account:
            frappe.throw(_("Bank Account {0} has no linked ledger account.").format(self.bank_account))
        return account


def employee_bank_details(employee):
    bank = frappe.db.get_value("Employee", employee, ["bank_name", "bank_ac_no"], as_dict=True) or {}
    if bank.get("bank_ac_no"):
        return _("Pay to {0} a/c {1}.").format(bank.get("bank_name") or "", bank.get("bank_ac_no"))
    return ""


def require_role(role):
    roles = frappe.get_roles()
    if role not in roles and "System Manager" not in roles:
        frappe.throw(_("Only a {0} can do this.").format(role), frappe.PermissionError)


# ----------------------------------------------------------------------
# Row-level access control. Wired via hooks.py.
# ----------------------------------------------------------------------


def get_permission_query_conditions(user=None):
    user = user or frappe.session.user
    roles = frappe.get_roles(user)
    if user == "Administrator" or {"System Manager", FINANCE, "CFO"} & set(roles):
        return ""
    conditions = []
    if TREASURY in roles:
        conditions.append("`tabLoan Disbursement`.status in ('Finance Verified', 'Treasury Released')")
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    if employee:
        conditions.append(f"`tabLoan Disbursement`.employee = {frappe.db.escape(employee)}")
    return "(" + " or ".join(conditions) + ")" if conditions else "1=0"


def has_permission(doc, ptype="read", user=None, debug=False):
    user = user or frappe.session.user
    roles = frappe.get_roles(user)
    if user == "Administrator" or {"System Manager", FINANCE, "CFO"} & set(roles):
        return True
    if TREASURY in roles and doc.status in ("Finance Verified", "Treasury Released"):
        return True
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    return bool(employee) and doc.employee == employee and ptype in ("read", "select", "print")
