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

# Document Events
# ---------------
# Salary Slip integration (payroll deduction, retrospective correction is
# a separate whitelisted call — see salary_slip_hooks.py) and the
# Employee-exit block.

doc_events = {
    "Salary Slip": {
        "validate": "employee_loan_lifecycle.salary_slip_hooks.preview_recoveries",
        "on_submit": "employee_loan_lifecycle.salary_slip_hooks.apply_recoveries",
        "on_cancel": "employee_loan_lifecycle.salary_slip_hooks.reverse_recoveries",
    },
    "Employee": {
        "validate": "employee_loan_lifecycle.salary_slip_hooks.block_exit_with_outstanding_loan",
    },
}

# Installation
# ------------
# No after_install/after_migrate hook — the "Loan Recovery" Salary
# Component salary_slip_hooks.py deducts against is created once by hand
# instead (Payroll > Salary Component > New, Type = Deduction, name it
# exactly "Loan Recovery"). See DECISIONS.md.

# Fixtures
# --------

# No Workflow fixture — both Loan Application and Loan Disbursement are
# driven entirely through whitelisted controller methods (take_action() /
# finance_verify() / treasury_release() / mark_disbursed() /
# cancel_disbursement()) plus client-script buttons, not the stock
# Workflow doctype. See DECISIONS.md §1 and the Loan Disbursement addendum
# for why: a Workflow's role-only "allowed" gate can't express the
# verifier-≠-releaser check or trigger Payment Entry creation, so a
# Workflow sitting alongside these methods let someone bypass both by
# using the Workflow's own action buttons instead.

fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["dt", "=", "Salary Slip"],
            ["fieldname", "=", "custom_loan_recoveries"],
        ],
    },
]