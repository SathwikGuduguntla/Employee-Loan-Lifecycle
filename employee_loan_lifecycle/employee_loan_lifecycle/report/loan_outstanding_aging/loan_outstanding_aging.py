import frappe
from frappe import _
from frappe.utils import getdate, add_days, get_first_day


def execute(filters=None):
    filters = frappe._dict(filters or {})
    as_on = getdate(filters.get("as_on_date") or getdate())

    columns = [
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
        {"label": _("Sanctioned"), "fieldname": "sanctioned",
         "fieldtype": "Currency", "width": 120},
        {"label": _("Disbursed"), "fieldname": "disbursed",
         "fieldtype": "Currency", "width": 120},
        {"label": _("Recovered"), "fieldname": "recovered",
         "fieldtype": "Currency", "width": 120},
        {"label": _("Outstanding Principal"), "fieldname": "outstanding_principal",
         "fieldtype": "Currency", "width": 160},
        {"label": _("Outstanding Interest"), "fieldname": "outstanding_interest",
         "fieldtype": "Currency", "width": 160},
        {"label": _("Arrears"), "fieldname": "arrears",
         "fieldtype": "Currency", "width": 120},
        {"label": _("Not Due"), "fieldname": "not_due",
         "fieldtype": "Currency", "width": 120},
        {"label": _("1–30 Days"), "fieldname": "age_1_30",
         "fieldtype": "Currency", "width": 120},
        {"label": _("31–60 Days"), "fieldname": "age_31_60",
         "fieldtype": "Currency", "width": 120},
        {"label": _("61–90 Days"), "fieldname": "age_61_90",
         "fieldtype": "Currency", "width": 120},
        {"label": _("Over 90 Days"), "fieldname": "age_over_90",
         "fieldtype": "Currency", "width": 130},
    ]

    conditions = ["l.docstatus = 1"]
    values = {"as_on": as_on}

    for key, column in [
        ("company", "l.company"),
        ("loan_type", "l.loan_type"),
        ("department", "e.department"),
    ]:
        if filters.get(key):
            conditions.append(f"{column} = %({key})s")
            values[key] = filters[key]

    loans = frappe.db.sql(
        f"""
        SELECT
            l.name AS loan,
            l.employee,
            l.employee_name,
            e.department,
            l.loan_type,
            l.sanctioned_amount AS sanctioned,
            l.disbursed_amount AS disbursed,
            (l.principal_recovered + l.interest_recovered) AS recovered,
            l.outstanding_principal,
            l.outstanding_interest,
            l.arrears_amount AS arrears
        FROM `tabLoan` l
        LEFT JOIN `tabEmployee` e ON e.name = l.employee
        WHERE {" AND ".join(conditions)}
        ORDER BY l.employee_name, l.name
        """,
        values,
        as_dict=True,
    )

    for loan in loans:
        buckets = {
            "not_due": 0,
            "age_1_30": 0,
            "age_31_60": 0,
            "age_61_90": 0,
            "age_over_90": 0,
        }

        schedules = frappe.get_all(
            "Loan Repayment Schedule",
            filters={"parent": loan.loan, "parenttype": "Loan",
                     "parentfield": "repayment_schedule"},
            fields=["due_date", "instalment_amount", "recovered_amount"],
        )

        for row in schedules:
            due = getdate(row.due_date)
            unpaid = max(
                (row.instalment_amount or 0) - (row.recovered_amount or 0),
                0,
            )
            if not unpaid:
                continue

            if due >= as_on:
                buckets["not_due"] += unpaid
                continue

            overdue_days = (as_on - due).days
            if overdue_days <= 30:
                buckets["age_1_30"] += unpaid
            elif overdue_days <= 60:
                buckets["age_31_60"] += unpaid
            elif overdue_days <= 90:
                buckets["age_61_90"] += unpaid
            else:
                buckets["age_over_90"] += unpaid

        loan.update(buckets)
        data_row_total = sum(buckets.values())
        # Aging is derived from schedule unpaid amounts; arrears_amount
        # remains the Loan's stored total arrears balance.

    return columns, loans