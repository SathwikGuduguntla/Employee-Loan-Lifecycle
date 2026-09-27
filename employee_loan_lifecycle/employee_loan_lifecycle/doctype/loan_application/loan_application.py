import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime, today


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

        # Resolve and freeze the approval chain at submit time. The chain
        # must not move later even if the Loan Approval Rule is edited
        # afterwards (a later edit affects only future submissions).
        self.resolve_approval_rule()
        self.status = "Pending Approval"
        self.current_step = 1

        if not self.sanctioned_amount:
            self.sanctioned_amount = self.requested_amount

    # ------------------------------------------------------------------
    # Approval matrix resolution
    # ------------------------------------------------------------------

    def resolve_approval_rule(self):
        rule = self.find_matching_rule()

        if not rule:
            frappe.throw(
                _(
                    "No Loan Approval Rule matches this company, loan type, "
                    "grade and amount. Ask HR to configure one before this "
                    "application can be submitted."
                )
            )

        if not rule.approvers:
            frappe.throw(
                _("Loan Approval Rule {0} has no approval steps configured.").format(rule.name)
            )

        if rule.require_board_resolution and not (self.board_resolution_ref or "").strip():
            frappe.throw(
                _("A board resolution reference is mandatory for this approval band.")
            )

        self.approval_rule = rule.name

    def find_matching_rule(self):
        """
        A rule matches if the amount falls in its band AND (its loan_type is
        blank OR equals this application's loan_type) AND (its
        employee_grade is blank OR equals this application's grade). Where
        more than one rule matches, the most specific one wins. A tie is a
        configuration error and is thrown rather than guessed at — see
        LoanApprovalRule.validate_no_overlap for the save-time check that
        should normally prevent this from ever happening at submit time.
        """
        amount = flt(self.requested_amount)

        candidates = frappe.get_all(
            "Loan Approval Rule",
            filters={
                "company": self.company,
                "min_amount": ["<=", amount],
                "max_amount": [">=", amount],
            },
            fields=["name", "loan_type", "employee_grade", "require_board_resolution"],
        )

        scored = []
        for row in candidates:
            score = 0

            if row.loan_type:
                if row.loan_type != self.loan_type:
                    continue
                score += 2

            if row.employee_grade:
                if row.employee_grade != self.employee_grade:
                    continue
                score += 1

            scored.append((score, row))

        if not scored:
            return None

        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_score = scored[0][0]
        tied = [row for score, row in scored if score == best_score]

        if len(tied) > 1:
            frappe.throw(
                _(
                    "Approval matrix is ambiguous for this request: {0} "
                    "rules are equally specific ({1}). Fix the Loan "
                    "Approval Rule configuration before resubmitting."
                ).format(len(tied), ", ".join(r.name for r in tied))
            )

        return frappe.get_doc("Loan Approval Rule", tied[0].name)

    def get_current_step(self, rule):
        for row in rule.approvers:
            if row.sequence == self.current_step:
                return row
        return None

    def is_valid_approver(self, step, user):
        if step.approver_user:
            if user == step.approver_user:
                return True, None
            if step.get("delegate_user") and user == step.delegate_user:
                if self.approver_on_leave(step.approver_user):
                    return True, step.approver_user
            return False, None

        if step.approver_role in frappe.get_roles(user):
            return True, None

        if step.get("delegate_user") and user == step.delegate_user:
            return True, None

        return False, None

    @staticmethod
    def approver_on_leave(user):
        employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
        if not employee:
            return False

        return bool(
            frappe.db.exists(
                "Leave Application",
                {
                    "employee": employee,
                    "status": "Approved",
                    "docstatus": 1,
                    "from_date": ["<=", today()],
                    "to_date": [">=", today()],
                },
            )
        )

    @frappe.whitelist()
    def take_action(self, action, comment=None):
        """
        Approve / Reject / Return the current step. This is the single
        entry point for moving an application through the chain — see
        DECISIONS.md for why this replaces the stock Workflow doctype.
        """
        self.check_permission("write")

        if self.docstatus != 1 or self.status != "Pending Approval":
            frappe.throw(_("This application is not currently pending approval."))

        if action not in ("Approved", "Rejected", "Returned"):
            frappe.throw(_("Unknown action: {0}").format(action))

        if action in ("Rejected", "Returned") and not (comment or "").strip():
            frappe.throw(_("A comment is mandatory when rejecting or returning."))

        rule = frappe.get_cached_doc("Loan Approval Rule", self.approval_rule)
        step = self.get_current_step(rule)

        if not step:
            frappe.throw(
                _("No approval step is configured at position {0}.").format(self.current_step)
            )

        user = frappe.session.user

        applicant_user = frappe.db.get_value("Employee", self.employee, "user_id")
        if applicant_user and user == applicant_user:
            frappe.throw(_("You cannot approve your own application."))

        already_acted = {row.approver for row in self.approval_log}
        if user in already_acted:
            frappe.throw(
                _("You have already acted on an earlier step of this application.")
            )

        is_valid, acted_for = self.is_valid_approver(step, user)
        if not is_valid:
            frappe.throw(
                _("You are not authorised to act on step {0} of this application.").format(
                    step.sequence
                ),
                frappe.PermissionError,
            )

        from_status = self.status
        final_approval = False

        if action == "Approved":
            if self.current_step >= len(rule.approvers):
                self.status = "Approved"
                final_approval = True
            else:
                self.current_step += 1
        elif action == "Rejected":
            self.status = "Rejected"
        elif action == "Returned":
            self.status = "Returned"
            self.current_step = 0

        self.append(
            "approval_log",
            {
                "step": step.sequence,
                "approver": user,
                "acted_for": acted_for,
                "action": action,
                "from_status": from_status,
                "to_status": self.status,
                "comment": comment,
                "acted_on": now_datetime(),
            },
        )

        self.save(ignore_permissions=True)

        if final_approval:
            self.create_loan()

        return self.status

    def create_loan(self):
        """
        The final approval is what actually originates the Loan. Terms are
        copied from this application + its Loan Type at this instant, so a
        later edit to Loan Type never retroactively changes an already
        sanctioned loan.
        """
        if self.loan:
            return  # already created — take_action must be idempotent.

        loan_type = frappe.get_doc("Loan Type", self.loan_type)

        loan = frappe.get_doc(
            {
                "doctype": "Loan",
                "loan_application": self.name,
                "employee": self.employee,
                "employee_name": self.employee_name,
                "company": self.company,
                "loan_type": self.loan_type,
                "interest_mode": loan_type.interest_mode,
                "rate_of_interest": loan_type.rate_of_interest,
                "tenure_months": self.tenure_months,
                "sanctioned_amount": self.sanctioned_amount or self.requested_amount,
                "first_deduction_month": self.first_deduction_month,
                "loan_account": loan_type.loan_account,
                "interest_account": loan_type.interest_account,
                "writeoff_account": loan_type.writeoff_account,
            }
        )
        loan.insert(ignore_permissions=True)
        loan.submit()

        self.db_set("loan", loan.name, update_modified=False)

    @frappe.whitelist()
    def resubmit(self):
        """A Returned application can be edited and resubmitted; the chain restarts."""
        self.check_permission("write")

        if self.status != "Returned":
            frappe.throw(_("Only a Returned application can be resubmitted."))

        self.evaluate_eligibility()
        if self.eligibility_status != "Eligible":
            frappe.throw(
                _("This application is not eligible: {0}").format(
                    self.eligibility_notes or "Eligibility checks failed."
                )
            )

        self.resolve_approval_rule()
        self.status = "Pending Approval"
        self.current_step = 1
        self.save(ignore_permissions=True)
        return self.status

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------

    def set_employee_details(self):
        if not self.employee:
            return

        employee = frappe.get_doc("Employee", self.employee)

        self.employee_name = employee.employee_name
        self.department = employee.department
        self.company = employee.company
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
            frappe.throw(_("Employees on probation cannot apply for a loan."))

        if self.is_serving_notice(employee):
            frappe.throw(_("Employees serving notice cannot apply for a loan."))

        if loan_type.disabled:
            failures.append(_("This Loan Type is disabled."))

        if loan_type.company and loan_type.company != self.company:
            failures.append(_("The selected Loan Type belongs to another company."))

        gross = self.get_monthly_gross()
        self.monthly_gross = gross or 0

        if gross is None or gross <= 0:
            failures.append(_("Could not determine a positive monthly gross salary."))
        else:
            cap = gross * (loan_type.max_multiple_of_gross or 0)
            if self.requested_amount > cap:
                failures.append(
                    _("Requested amount exceeds the salary-based cap of {0}.").format(
                        frappe.format_value(cap, {"fieldtype": "Currency"})
                    )
                )

        max_tenure = loan_type.max_tenure_months or 0
        if max_tenure and self.tenure_months > max_tenure:
            failures.append(
                _("Tenure exceeds the maximum of {0} months.").format(max_tenure)
            )

        joining_date = getdate(employee.date_of_joining) if employee.date_of_joining else None
        if not joining_date:
            failures.append(_("Employee joining date is missing."))
        else:
            service_months = self.months_of_service(joining_date, getdate(today()))
            minimum = loan_type.min_service_months or 0
            if service_months < minimum:
                failures.append(
                    _("Employee has {0} months of service; {1} are required.").format(
                        service_months, minimum
                    )
                )

        max_active = loan_type.max_active_loans or 0
        if max_active:
            active_count = self.count_active_loans()
            if active_count >= max_active:
                failures.append(
                    _("Employee already has the maximum {0} active loan(s) of this type.").format(
                        max_active
                    )
                )

        if self.eligibility_override:
            if "HR Manager" not in frappe.get_roles():
                frappe.throw(_("Only an HR Manager can override eligibility."))
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
            self.eligibility_notes = _("All eligibility checks passed.")

    def get_monthly_gross(self):
        reference_date = getdate(today())

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
        months = (end_date.year - start_date.year) * 12 + end_date.month - start_date.month
        if end_date.day < start_date.day:
            months -= 1
        return max(months, 0)

    @staticmethod
    def is_on_probation(employee):
        return bool(getattr(employee, "custom_on_probation", 0))

    @staticmethod
    def is_serving_notice(employee):
        return bool(getattr(employee, "custom_serving_notice", 0))


# ----------------------------------------------------------------------
# Row-level access control (layer 2 of 3). Wired via hooks.py
# permission_query_conditions / has_permission for "Loan Application".
# ----------------------------------------------------------------------


def get_permission_query_conditions(user=None):
    user = user or frappe.session.user

    if "System Manager" in frappe.get_roles(user):
        return ""

    conditions = []
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")

    if employee:
        conditions.append(f"`tabLoan Application`.employee = {frappe.db.escape(employee)}")
        conditions.append(
            f"""`tabLoan Application`.employee in (
                select name from `tabEmployee` where reports_to = {frappe.db.escape(employee)}
            )"""
        )

    roles = frappe.get_roles(user)
    role_list = ", ".join(frappe.db.escape(r) for r in roles) or "''"

    conditions.append(
        f"""exists (
            select 1 from `tabLoan Approval Step` step
            where step.parent = `tabLoan Application`.approval_rule
              and step.sequence = `tabLoan Application`.current_step
              and (
                    step.approver_role in ({role_list})
                    or step.approver_user = {frappe.db.escape(user)}
                    or step.delegate_user = {frappe.db.escape(user)}
              )
        )"""
    )

    conditions.append(
        f"""exists (
            select 1 from `tabLoan Approval Log` log
            where log.parent = `tabLoan Application`.name
              and (log.approver = {frappe.db.escape(user)} or log.acted_for = {frappe.db.escape(user)})
        )"""
    )

    return "(" + " or ".join(conditions) + ")"


def has_permission(doc, ptype="read", user=None):
    user = user or frappe.session.user

    if "System Manager" in frappe.get_roles(user):
        return True

    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")

    if employee and doc.employee == employee:
        return True

    if employee and frappe.db.get_value("Employee", doc.employee, "reports_to") == employee:
        return True

    if any(row.approver == user or row.acted_for == user for row in doc.approval_log):
        return True

    if doc.approval_rule and doc.current_step:
        step = frappe.db.get_value(
            "Loan Approval Step",
            {"parent": doc.approval_rule, "sequence": doc.current_step},
            ["approver_role", "approver_user", "delegate_user"],
            as_dict=True,
        )
        if step:
            if user in (step.approver_user, step.delegate_user):
                return True
            if step.approver_role and step.approver_role in frappe.get_roles(user):
                return True

    return False