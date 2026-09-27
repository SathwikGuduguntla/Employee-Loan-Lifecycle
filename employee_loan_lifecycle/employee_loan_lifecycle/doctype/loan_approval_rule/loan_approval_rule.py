# Copyright (c) 2026, sathwik and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class LoanApprovalRule(Document):
    def validate(self):
        self.validate_amount_band()
        self.validate_steps()
        self.validate_no_overlap()

    def validate_amount_band(self):
        if flt(self.min_amount) < 0 or flt(self.max_amount) <= 0:
            frappe.throw(_("Min/Max amount must be positive."))

        if flt(self.min_amount) > flt(self.max_amount):
            frappe.throw(_("Min Amount cannot be greater than Max Amount."))

    def validate_steps(self):
        if not self.approvers:
            frappe.throw(_("Add at least one approval step."))

        seen_sequences = set()

        for row in self.approvers:
            if not row.approver_role and not row.approver_user:
                frappe.throw(
                    _("Row {0}: set either an Approver Role or an Approver User.").format(
                        row.idx
                    )
                )

            if row.approver_role and row.approver_user:
                frappe.throw(
                    _(
                        "Row {0}: set either an Approver Role or an Approver User, "
                        "not both — a named-user step cannot also be satisfied by "
                        "anyone holding a role."
                    ).format(row.idx)
                )

            if row.delegate_user and not row.approver_user:
                frappe.throw(
                    _(
                        "Row {0}: a Delegate User only makes sense on a "
                        "named-approver (Approver User) step."
                    ).format(row.idx)
                )

            if row.sequence in seen_sequences:
                frappe.throw(
                    _("Row {0}: sequence {1} is used more than once.").format(
                        row.idx, row.sequence
                    )
                )
            seen_sequences.add(row.sequence)

        expected = set(range(1, len(self.approvers) + 1))
        if seen_sequences != expected:
            frappe.throw(
                _("Approval steps must be numbered 1..{0} with no gaps or duplicates.").format(
                    len(self.approvers)
                )
            )

    def validate_no_overlap(self):
        """
        Two rules that would tie in specificity for the same company and an
        overlapping amount band is a configuration error: at submission
        time there would be no way to pick between them. Caught here, at
        configuration time, instead of surfacing as a runtime throw on
        someone's loan application.
        """
        others = frappe.get_all(
            "Loan Approval Rule",
            filters={
                "company": self.company,
                "name": ["!=", self.name or ""],
                "loan_type": self.loan_type or "",
                "employee_grade": self.employee_grade or "",
            },
            fields=["name", "min_amount", "max_amount"],
        )

        for other in others:
            if flt(self.min_amount) <= flt(other.max_amount) and flt(
                self.max_amount
            ) >= flt(other.min_amount):
                frappe.throw(
                    _(
                        "This rule's amount band overlaps {0}, which has the same "
                        "Loan Type and Employee Grade. Narrow the bands so they "
                        "don't tie."
                    ).format(other.name)
                )
