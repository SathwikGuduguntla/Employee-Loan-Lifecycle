app_name = "employee_loan_lifecycle"
app_title = "Employee Loan Lifecycle"
app_publisher = "sathwik"
app_description = "which gives loans to employees "
app_email = "guguntlasathwik@gmail.com"
app_license = "agpl-3.0"

# Apps
# ------------------

# required_apps = []

# Permissions
# -----------
# Row-level access control (layer 2 of the 3-layer requirement — layer 1
# is the doctype-level permissions rows in each doctype's JSON, layer 3 is
# the permlevel on Loan Application's sanctioned_amount / eligibility
# override fields). See DECISIONS.md for the full write-up.

permission_query_conditions = {
    "Loan": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan.get_permission_query_conditions",
    "Loan Application": "employee_loan_lifecycle.employee_loan_lifecycle.doctype."
    "loan_application.loan_application.get_permission_query_conditions",
    "Loan Disbursement": "employee_loan_lifecycle.employee_loan_lifecycle.doctype."
    "loan_disbursement.loan_disbursement.get_permission_query_conditions",
    "Loan Repayment": "employee_loan_lifecycle.employee_loan_lifecycle.doctype."
    "loan_repayment.loan_repayment.get_permission_query_conditions",
}

has_permission = {
    "Loan": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan.has_permission",
    "Loan Application": "employee_loan_lifecycle.employee_loan_lifecycle.doctype."
    "loan_application.loan_application.has_permission",
    "Loan Disbursement": "employee_loan_lifecycle.employee_loan_lifecycle.doctype."
    "loan_disbursement.loan_disbursement.has_permission",
    "Loan Repayment": "employee_loan_lifecycle.employee_loan_lifecycle.doctype."
    "loan_repayment.loan_repayment.has_permission",
}

# Salary Slip
# -----------
# The payroll deduction lives in an override class rather than doc_events:
# it has to run *inside* HRMS's net pay calculation (set_net_pay), which no
# document event reaches. See DECISIONS.md §6.

override_doctype_class = {
    "Salary Slip": "employee_loan_lifecycle.overrides.salary_slip.LoanSalarySlip",
}

doc_events = {
    "Employee": {
        "validate": "employee_loan_lifecycle.overrides.employee.block_exit_with_outstanding_loan",
    },
}

# Fixtures
# --------
# Loan Application approvals run through LoanApplication.take_action()
# against the Loan Approval Rule matrix, and Loan Disbursement through its
# own finance_verify() / treasury_release() methods, so no Workflow is
# shipped (DECISIONS.md §1).

fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["dt", "=", "Salary Slip"],
            ["fieldname", "in", ["loan_recovery_section", "loan_recoveries", "total_loan_repayment", "loan_journal_entry"]],
        ],
    },
    {
        "dt": "Role",
        "filters": [["name", "in", ["Reporting Manager", "Finance Manager", "Treasury Officer", "CFO"]]],
    },
]
