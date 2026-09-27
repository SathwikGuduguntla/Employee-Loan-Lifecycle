import frappe
from frappe import _
from frappe.utils import getdate


def execute(filters=None):
    filters = frappe._dict(filters or {})
    as_on = getdate(filters.get("as_on_date") or getdate())

    columns = [
        {"label": _("Month"), "fieldname": "month", "fieldtype": "Data",
         "width": 100},
        {"label": _("Loan"), "fieldname": "loan", "fieldtype": "Link",
         "options": "Loan", "width": 150},
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link",
         "options": "Employee", "width": 130},
        {"label": _("Employee Name"), "fieldname": "employee_name",
         "fieldtype": "Data", "width": 180},
        {"label": _("Department"), "fieldname": "department",
         "fieldtype": "Link", "options": "Department", "width": 140},
        {"label": _("Loan Type"), "fieldname": "loan_type",
         "fieldtype": "Link", "options": "Loan Type", "width": 140},
        {"label": _("Period"), "fieldname": "period_no",
         "fieldtype": "Int", "width": 70},
        {"label": _("Due Date"), "fieldname": "due_date",
         "fieldtype": "Date", "width": 100},
        {"label": _("Scheduled"), "fieldname": "scheduled",
         "fieldtype": "Currency", "width": 120},
        {"label": _("Actual Recovered"), "fieldname": "actual",
         "fieldtype": "Currency", "width": 140},
        {"label": _("Monthly Shortfall"), "fieldname": "shortfall",
         "fieldtype": "Currency", "width": 130},
        {"label": _("Running Shortfall"), "fieldname": "running_shortfall",
         "fieldtype": "Currency", "width": 140},
    ]

    conditions = ["l.docstatus = 1", "s.due_date <= %(as_on)s"]
    values = {"as_on": as_on}

    for key, column in [
        ("company", "l.company"),
        ("loan_type", "l.loan_type"),
        ("department", "e.department"),
    ]:
        if filters.get(key):
            conditions.append(f"{column} = %({key})s")
            values[key] = filters[key]

    rows = frappe.db.sql(
        f"""
        SELECT
            l.name AS loan,
            l.employee,
            l.employee_name,
            e.department,
            l.loan_type,
            s.period_no,
            s.due_date,
            s.instalment_amount AS scheduled,
            COALESCE(SUM(r.total_recovered), 0) AS actual
        FROM `tabLoan` l
        INNER JOIN `tabLoan Repayment Schedule` s
            ON s.parent = l.name
            AND s.parenttype = 'Loan'
            AND s.parentfield = 'repayment_schedule'
        LEFT JOIN `tabEmployee` e
            ON e.name = l.employee
        LEFT JOIN `tabSalary Slip` ss
            ON ss.employee = l.employee
            AND ss.docstatus = 1
        LEFT JOIN `tabLoan Recovery Detail` r
            ON r.parent = ss.name
            AND r.parenttype = 'Salary Slip'
            AND r.parentfield = 'loan_recoveries'
            AND r.loan = l.name
            AND r.schedule_period = s.period_no
        WHERE {" AND ".join(conditions)}
        GROUP BY
            l.name, l.employee, l.employee_name, e.department,
            l.loan_type, s.period_no, s.due_date, s.instalment_amount
        ORDER BY l.employee_name, l.name, s.due_date
        """,
        values,
        as_dict=True,
    )

    running = {}
    for row in rows:
        key = row.loan
        shortfall = (row.scheduled or 0) - (row.actual or 0)
        running[key] = running.get(key, 0) + shortfall

        due = getdate(row.due_date)
        row.month = due.strftime("%Y-%m")
        row.shortfall = shortfall
        row.running_shortfall = running[key]

    return columns, rows