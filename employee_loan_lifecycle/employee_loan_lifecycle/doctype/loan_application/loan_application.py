
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today


class LoanApplication(Document):
    def validate(self):
        self.set_employee_details()
        self.validate_request()
        self.evaluate_eligibility()

    def before_submit(self):
        # Re-evaluate immediately before the application enters approval.
        self.evaluate_eligibility()

        if self.eligibility_status != "Eligible":
            frappe.throw(
                _("This application is not eligible: {0}").format(
                    self.eligibility_notes or "Eligibility checks failed."
                )
            )

    def set_employee_details(self):
        if not self.employee:
            return

        employee = frappe.get_doc("Employee", self.employee)

        self.employee_name = employee.employee_name
        self.department = employee.department
        self.company = employee.company

        # Replace with the actual grade field if different.
        self.employee_grade = getattr(employee, "grade", None)

    def validate_request(self):
        if not self.employee:
            frappe.throw(_("Employee is required."))

        if not self.company:
            frappe.throw(_("Company is required."))

        if not self.loan_type:
            frappe.throw(_("Loan Type is required."))

        if not self.requested_amount or self.requested_amount <= 0:
            frappe.throw(_("Requested amount must be greater than zero."))

        if not self.tenure_months or self.tenure_months <= 0:
            frappe.throw(_("Tenure must be greater than zero."))

        if not self.first_deduction_month:
            frappe.throw(_("First deduction month is required."))

    def evaluate_eligibility(self):
        failures = []

        employee = frappe.get_doc("Employee", self.employee)
        loan_type = frappe.get_doc("Loan Type", self.loan_type)

        if employee.status != "Active":
            failures.append(_("Employee is not Active."))

        if self.is_on_probation(employee):
            frappe.throw(
                _("Employees on probation cannot apply for a loan.")
            )

        if self.is_serving_notice(employee):
            frappe.throw(
                _("Employees serving notice cannot apply for a loan.")
            )

        if loan_type.disabled:
            failures.append(_("This Loan Type is disabled."))

        if loan_type.company and loan_type.company != self.company:
            failures.append(
                _("The selected Loan Type belongs to another company.")
            )

        # Salary snapshot.
        gross = self.get_monthly_gross()
        self.monthly_gross = gross or 0

        if gross is None or gross <= 0:
            failures.append(
                _("Could not determine a positive monthly gross salary.")
            )
        else:
            cap = gross * (loan_type.max_multiple_of_gross or 0)

            if self.requested_amount > cap:
                failures.append(
                    _("Requested amount exceeds the salary-based cap of {0}.").format(
                        frappe.format_value(
                            cap, {"fieldtype": "Currency"}
                        )
                    )
                )

        # Tenure limit.
        max_tenure = loan_type.max_tenure_months or 0

        if max_tenure and self.tenure_months > max_tenure:
            failures.append(
                _("Tenure exceeds the maximum of {0} months.").format(
                    max_tenure
                )
            )

        # Minimum service period.
        joining_date = (
            getdate(employee.date_of_joining)
            if employee.date_of_joining
            else None
        )

        if not joining_date:
            failures.append(_("Employee joining date is missing."))
        else:
            service_months = self.months_of_service(
                joining_date, getdate(today())
            )
            minimum = loan_type.min_service_months or 0

            if service_months < minimum:
                failures.append(
                    _("Employee has {0} months of service; {1} are required.").format(
                        service_months, minimum
                    )
                )

        # Maximum active loans of this type.
        max_active = loan_type.max_active_loans or 0

        if max_active:
            active_count = self.count_active_loans()

            if active_count >= max_active:
                failures.append(
                    _("Employee already has the maximum {0} active loan(s) of this type.").format(
                        max_active
                    )
                )

        # HR override applies to eligibility failures, but not
        # the hard probation/notice prohibition above.
        if self.eligibility_override:
            if "HR Manager" not in frappe.get_roles():
                frappe.throw(
                    _("Only an HR Manager can override eligibility.")
                )

            if not self.override_reason or not self.override_reason.strip():
                frappe.throw(_("Override reason is mandatory."))

            self.eligibility_status = "Eligible"
            self.eligibility_notes = _(
                "Eligibility overridden by HR Manager: {0}"
            ).format(self.override_reason)
            return

        if failures:
            self.eligibility_status = "Ineligible"
            self.eligibility_notes = "\n".join(failures)
        else:
            self.eligibility_status = "Eligible"
            self.eligibility_notes = _(
                "All eligibility checks passed."
            )

    def get_monthly_gross(self):
        reference_date = getdate(today())

        # First choice: latest submitted Salary Slip
        # effective on or before the reference date.
        salary_slips = frappe.get_all(
            "Salary Slip",
            filters={
                "employee": self.employee,
                "company": self.company,
                "docstatus": 1,
                "start_date": ["<=", reference_date],
            },
            fields=["gross_pay", "start_date"],
            order_by="start_date desc",
            limit=1,
        )

        if salary_slips:
            gross = salary_slips[0].gross_pay
            if gross and gross > 0:
                return gross

        # Fallback: latest submitted, effective Salary
        # Structure Assignment.
        assignments = frappe.get_all(
            "Salary Structure Assignment",
            filters={
                "employee": self.employee,
                "company": self.company,
                "docstatus": 1,
                "from_date": ["<=", reference_date],
            },
            fields=["name", "base", "from_date"],
            order_by="from_date desc",
            limit=1,
        )

        if assignments:
            base = assignments[0].base
            if base and base > 0:
                # This assumes the assignment base is monthly gross.
                return base

        return None

    def count_active_loans(self):
        return frappe.db.count(
            "Loan",
            filters={
                "employee": self.employee,
                "loan_type": self.loan_type,
                "status": ["not in", ["Closed", "Written Off"]],
            },
        )

    @staticmethod
    def months_of_service(start_date, end_date):
        months = (
            (end_date.year - start_date.year) * 12
            + end_date.month
            - start_date.month
        )

        if end_date.day < start_date.day:
            months -= 1

        return max(months, 0)

    @staticmethod
    def is_on_probation(employee):
        # Replace with your actual HRMS probation field/rule.
        return bool(
            getattr(employee, "custom_on_probation", 0)
        )

    @staticmethod
    def is_serving_notice(employee):
        # Replace with your actual notice-period field/rule.
        return bool(
            getattr(employee, "custom_serving_notice", 0)
        )