import frappe
from frappe import _
from frappe.utils import flt, fmt_money


def block_exit_with_outstanding_loan(doc, method=None):
    """Employee.validate: nobody is marked Left while they still owe money."""
    if doc.status != "Left" or not doc.has_value_changed("status"):
        return

    loans = frappe.get_all(
        "Loan",
        filters={"employee": doc.name, "docstatus": 1, "status": ["not in", ("Closed", "Written Off")]},
        fields=["name", "outstanding_principal", "sanctioned_amount", "disbursed_amount", "company"],
    )
    if not loans:
        return

    lines = [
        _("{0}: {1} outstanding").format(
            loan.name,
            fmt_money(flt(loan.outstanding_principal), currency=frappe.get_cached_value("Company", loan.company, "default_currency")),
        )
        for loan in loans
    ]
    frappe.throw(
        _(
            "{0} cannot be marked Left while loans are open. Recover the balance from the final settlement "
            "(a Foreclosure repayment) or write it off first:<br>{1}"
        ).format(doc.employee_name or doc.name, "<br>".join(lines)),
        title=_("Outstanding Loan"),
    )
