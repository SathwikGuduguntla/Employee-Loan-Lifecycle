app_name = "employee_loan_lifecycle"
app_title = "Employee Loan Lifecycle"
app_publisher = "sathwik"
app_description = "which gives loans to employees"
app_email = "guguntlasathwik@gmail.com"
app_license = "agpl-3.0"

permission_query_conditions = {
    "Loan": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan.get_permission_query_conditions",
    "Loan Application": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_application.loan_application.get_permission_query_conditions",
    "Loan Disbursement": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_disbursement.loan_disbursement.get_permission_query_conditions",
    "Loan Repayment": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_repayment.loan_repayment.get_permission_query_conditions",
}

has_permission = {
    "Loan": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan.loan.has_permission",
    "Loan Application": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_application.loan_application.has_permission",
    "Loan Disbursement": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_disbursement.loan_disbursement.has_permission",
    "Loan Repayment": "employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_repayment.loan_repayment.has_permission",
}

doc_events = {}

override_doctype_class = {
    "Salary Slip": "employee_loan_lifecycle.overrides.salary_slip.LoanSalarySlip",
}

fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["dt", "=", "Salary Slip"],
            ["fieldname", "=", "custom_loan_recoveries"],
        ],
    },
]