# Test data for the Employee Loan Lifecycle app. Run: bench --site <site> execute employee_loan_lifecycle.seed_test_data.run
# Creates the Section 03 masters from the brief at a small scale so it runs fast.
# Re-running is safe: existing records are skipped.
import random
import frappe
from frappe.utils import add_months, get_first_day, get_last_day, getdate, nowdate

N_EMPLOYEES = 60        # brief: 2000 (slow on a laptop)
SLIP_MONTHS = 2         # brief: 18 (each month = one salary slip per employee)
N_LOANS = 12            # brief: 400
PASSWORD = "Test@1234"
random.seed(42)

DEPARTMENTS = ["Production", "Quality", "Maintenance", "Stores", "Purchase", "Sales", "Marketing",
               "Finance", "Human Resources", "IT", "Logistics", "Safety", "R&D", "Administration"]
GRADES = {"G1": 25000, "G2": 40000, "G3": 60000, "G4": 90000, "G5": 150000}  # monthly base
ROLE_USERS = {  # role -> (user email, employee first name, department)
    "Reporting Manager": ("rm@test.com", "Ravi", "Production"),
    "HR Manager": ("hr@test.com", "Hema", "Human Resources"),
    "Finance Manager": ("finance@test.com", "Farhan", "Finance"),
    "Treasury Officer": ("treasury@test.com", "Tara", "Finance"),
    "CFO": ("cfo@test.com", "Chitra", "Finance"),
}


def ensure(doctype, name, values, set_name=None, submit=False):
    if frappe.db.exists(doctype, name):
        return frappe.get_doc(doctype, name)
    doc = frappe.get_doc({"doctype": doctype, **values})
    doc.flags.ignore_permissions = True
    doc.insert(set_name=set_name)
    if submit:
        doc.submit()
    return doc


def acc(company, name):
    return f"{name} - {frappe.get_cached_value('Company', company, 'abbr')}"


def setup_company(company, abbr=None):
    if not frappe.db.exists("Company", company):
        ensure("Company", company, {"company_name": company, "abbr": abbr, "default_currency": "INR",
                                     "country": "India", "chart_of_accounts": "Standard"})
    for name, parent, root, acct_type in [
        ("Employee Loan Receivable", "Loans and Advances (Assets)", "Asset", "Receivable"),
        ("Interest Income on Staff Loans", "Indirect Income", "Income", ""),
        ("Staff Loan Written Off", "Indirect Expenses", "Expense", ""),
        ("Staff Loan Bank", "Bank Accounts", "Asset", "Bank"),
    ]:
        ensure("Account", acc(company, name), {"account_name": name, "company": company,
               "parent_account": acc(company, parent), "root_type": root, "account_type": acct_type})
    bank = ensure("Bank", "Test Bank", {"bank_name": "Test Bank"})
    ba = frappe.db.get_value("Bank Account", {"company": company, "is_company_account": 1}, "name")
    if not ba:
        ba = ensure("Bank Account", "__new__", {"account_name": f"Staff Loans {frappe.get_cached_value('Company', company, 'abbr')}", "bank": bank.name,
                    "company": company, "is_company_account": 1,
                    "account": acc(company, "Staff Loan Bank"), "bank_account_no": "00" + str(random.randint(10**8, 10**9))}).name
    mop = frappe.get_doc("Mode of Payment", "Wire Transfer")
    if not any(r.company == company for r in mop.accounts):
        mop.append("accounts", {"company": company, "default_account": acc(company, "Staff Loan Bank")})
        mop.save(ignore_permissions=True)
    hl = ensure("Holiday List", f"Holidays {company}", {"holiday_list_name": f"Holidays {company}",
                "from_date": "2024-01-01", "to_date": "2027-12-31", "weekly_off": "Sunday"})
    if not hl.holidays:
        hl.get_weekly_off_dates()
        hl.save(ignore_permissions=True)
    # HRMS v16 resolves holidays through Holiday List Assignment, not the company default.
    if frappe.db.exists("DocType", "Holiday List Assignment") and not frappe.db.exists(
        "Holiday List Assignment", {"assigned_to": company, "docstatus": 1}
    ):
        hla = frappe.get_doc({"doctype": "Holiday List Assignment", "applicable_for": "Company",
                              "assigned_to": company, "holiday_list": hl.name, "from_date": "2024-01-01"})
        hla.insert(ignore_permissions=True)
        hla.submit()
    frappe.db.set_value("Company", company, {
        "default_holiday_list": hl.name,
        "default_bank_account": acc(company, "Staff Loan Bank"),
        "default_payroll_payable_account": frappe.db.get_value("Company", company, "default_payroll_payable_account")
        or acc(company, "Payroll Payable"),
    })
    return ba


def setup_loan_masters(company, suffix=""):
    types = {}
    for name, mode, rate, tenure, multiple, charge in [
        ("Personal", "Flat", 8, 36, 3, 2), ("Housing", "Reducing Balance", 6, 120, 20, 1),
        ("Emergency", "Flat", 0, 6, 1, 0)]:
        tname = name + suffix
        ensure("Loan Type", tname, {"loan_type_name": tname, "company": company, "interest_mode": mode,
               "rate_of_interest": rate, "max_tenure_months": tenure, "max_multiple_of_gross": multiple,
               "min_service_months": 6, "max_active_loans": 1, "pre_closure_charge_percent": charge,
               "loan_account": acc(company, "Employee Loan Receivable"),
               "interest_account": acc(company, "Interest Income on Staff Loans"),
               "writeoff_account": acc(company, "Staff Loan Written Off")}, set_name=tname)
        types[name] = tname

    chain = ["Reporting Manager", "HR Manager", "Finance Manager", "CFO"]
    for band, lo, hi, steps, board in [("B1", 0, 50000, 2, 0), ("B2", 50001, 200000, 3, 0),
                                       ("B3", 200001, 1000000, 4, 0), ("B4", 1000001, 999999999, 4, 1)]:
        ensure("Loan Approval Rule", band + suffix, {"company": company, "min_amount": lo, "max_amount": hi,
               "require_board_resolution": board,
               "approvers": [{"sequence": i + 1, "approver_role": r} for i, r in enumerate(chain[:steps])]},
               set_name=band + suffix)
    # A row that names a specific person instead of a role (brief Part A 01).
    ensure("Loan Approval Rule", "B3-HOUSING-G5" + suffix, {"company": company, "loan_type": types["Housing"],
           "employee_grade": "G5", "min_amount": 200001, "max_amount": 1000000,
           "approvers": [{"sequence": 1, "approver_role": "Reporting Manager"},
                         {"sequence": 2, "approver_role": "HR Manager"},
                         {"sequence": 3, "approver_user": "finance@test.com"},
                         {"sequence": 4, "approver_user": "cfo@test.com", "delegate_user": "finance@test.com"}]},
           set_name="B3-HOUSING-G5" + suffix)
    return types


def setup_hr_masters(companies):
    for d in DEPARTMENTS:
        for c in companies:
            abbr = frappe.get_cached_value("Company", c, "abbr")
            ensure("Department", f"{d} - {abbr}", {"department_name": d, "company": c,
                   "parent_department": "All Departments"})
    for g, base in GRADES.items():
        ensure("Employee Grade", g, {"default_base_pay": base}, set_name=g)
    ensure("Designation", "Staff", {"designation_name": "Staff"})
    for comp, ctype, formula in [("Basic", "Earning", "base * .6"), ("HRA", "Earning", "base * .4"),
                                 ("Professional Tax", "Deduction", "200")]:
        ensure("Salary Component", comp, {"salary_component": comp, "type": ctype,
               "salary_component_abbr": "".join(w[0] for w in comp.split()) or comp[:2]})
    for c in companies:
        ensure("Salary Structure", f"Staff Structure - {c}", {"name": f"Staff Structure - {c}", "company": c,
               "payroll_frequency": "Monthly", "currency": "INR", "is_active": "Yes",
               "earnings": [{"salary_component": "Basic", "abbr": "B", "amount_based_on_formula": 1, "formula": "base * .6"},
                            {"salary_component": "HRA", "abbr": "H", "amount_based_on_formula": 1, "formula": "base * .4"}],
               "deductions": [{"salary_component": "Professional Tax", "abbr": "PT", "amount": 200}]},
               set_name=f"Staff Structure - {c}", submit=True)
    ps = frappe.get_single("Payroll Settings")
    ps.payroll_based_on = "Leave"
    ps.email_salary_slip_to_employee = 0
    ps.save(ignore_permissions=True)


def make_employee(first, company, dept, grade, reports_to=None, user=None, joined=None):
    abbr = frappe.get_cached_value("Company", company, "abbr")
    existing = frappe.db.get_value("Employee", {"first_name": first, "company": company}, "name")
    if existing:
        return existing
    emp = frappe.get_doc({"doctype": "Employee", "first_name": first, "last_name": "Test",
        "gender": random.choice(["Male", "Female"]), "date_of_birth": "1990-01-15",
        "date_of_joining": joined or add_months(getdate(nowdate()), -random.randint(12, 72)),
        "company": company, "department": f"{dept} - {abbr}", "grade": grade, "designation": "Staff",
        "status": "Active", "reports_to": reports_to, "user_id": user,
        "bank_name": "Test Bank", "bank_ac_no": str(random.randint(10**9, 10**10))})
    emp.insert(ignore_permissions=True)
    return emp.name


def make_user(email, first, role):
    if not frappe.db.exists("User", email):
        u = frappe.get_doc({"doctype": "User", "email": email, "first_name": first, "send_welcome_email": 0,
                            "new_password": PASSWORD, "roles": [{"role": role}, {"role": "Employee"}]})
        u.insert(ignore_permissions=True)
    return email


def assign_salary(emp, company, grade, from_date):
    if frappe.db.exists("Salary Structure Assignment", {"employee": emp, "docstatus": 1}):
        return
    ssa = frappe.get_doc({"doctype": "Salary Structure Assignment", "employee": emp, "company": company,
          "salary_structure": f"Staff Structure - {company}", "from_date": from_date, "currency": "INR",
          "base": GRADES[grade] * random.uniform(0.9, 1.1),
          "payroll_payable_account": frappe.db.get_value("Company", company, "default_payroll_payable_account")})
    ssa.insert(ignore_permissions=True)
    ssa.submit()


def run():
    c1 = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
    c2 = "Acme Components"
    for fy in ("2024-2025", "2025-2026", "2026-2027", "2027-2028"):
        y = int(fy[:4])
        ensure("Fiscal Year", fy, {"year": fy, "year_start_date": f"{y}-04-01", "year_end_date": f"{y+1}-03-31"})
    setup_company(c1)
    setup_company(c2, "AC")
    print("companies, accounts, bank, holiday lists: ok")

    setup_hr_masters([c1, c2])
    print("departments, grades, salary structures: ok")

    start = get_first_day(add_months(getdate(nowdate()), -SLIP_MONTHS))

    # Role users, each linked to an employee (HR scope = own department; RM = own reports).
    ceo = make_employee("Chief", c1, "Administration", "G5", joined="2018-01-01")
    role_emp = {}
    for role, (email, first, dept) in ROLE_USERS.items():
        make_user(email, first, role)
        role_emp[role] = make_employee(first, c1, dept, "G5", reports_to=ceo, user=email, joined="2019-01-01")
    for e in [ceo, *role_emp.values()]:
        assign_salary(e, c1, "G5", start)
    print("role users: " + ", ".join(e for e, *_ in ROLE_USERS.values()) + f" (password {PASSWORD})")

    employees = []
    for i in range(N_EMPLOYEES):
        company = c1 if i % 5 else c2
        grade = random.choice(list(GRADES))
        manager = role_emp["Reporting Manager"] if company == c1 and i % 3 == 0 else None
        emp = make_employee(f"Emp{i:04d}", company, random.choice(DEPARTMENTS), grade, reports_to=manager)
        assign_salary(emp, company, grade, start)
        employees.append((emp, company, grade))
    frappe.db.commit()
    print(f"{len(employees)} employees with salary assignments: ok")

    types = {c1: setup_loan_masters(c1), c2: setup_loan_masters(c2, " - AC")}
    print("loan types + approval matrix (B1-B4 and a named-approver row): ok")

    # Leave: allocations, plus the Finance Manager on approved leave today (for delegation tests).
    for emp, company, _ in employees[:20] + [(role_emp["Finance Manager"], c1, "G5")]:
        ensure("Leave Allocation", frappe.db.get_value("Leave Allocation", {"employee": emp}, "name") or "__new__",
               {"employee": emp, "leave_type": "Casual Leave", "from_date": start,
                "to_date": add_months(start, 12), "new_leaves_allocated": 12}, submit=True)
    fm = role_emp["Finance Manager"]
    if not frappe.db.exists("Leave Application", {"employee": fm, "docstatus": 1}):
        la = frappe.get_doc({"doctype": "Leave Application", "employee": fm, "leave_type": "Casual Leave",
                             "from_date": nowdate(), "to_date": nowdate(), "status": "Approved",
                             "leave_approver": "Administrator"})
        la.insert(ignore_permissions=True)
        la.submit()
    print("leave allocations + Finance Manager on leave today: ok")

    # Salary slips for past months (history for eligibility's monthly gross).
    made = 0
    for m in range(SLIP_MONTHS):
        s = add_months(start, m)
        for emp, company, _ in employees:
            if frappe.db.exists("Salary Slip", {"employee": emp, "start_date": s, "docstatus": 1}):
                continue
            slip = frappe.get_doc({"doctype": "Salary Slip", "employee": emp, "company": company,
                                   "posting_date": get_last_day(s), "start_date": s, "end_date": get_last_day(s),
                                   "payroll_frequency": "Monthly"})
            slip.insert(ignore_permissions=True)
            slip.submit()
            made += 1
        frappe.db.commit()
    print(f"{made} salary slips submitted: ok")

    # Sanctioned loans (the "existing loans" of the brief), created directly.
    loans = 0
    for emp, company, grade in employees[:N_LOANS]:
        if frappe.db.exists("Loan", {"employee": emp}):
            continue
        kind = ["Personal", "Housing", "Emergency"][loans % 3]
        lt = frappe.get_doc("Loan Type", types[company][kind])
        loan = frappe.get_doc({"doctype": "Loan", "employee": emp, "company": company, "loan_type": lt.name,
            "interest_mode": lt.interest_mode, "rate_of_interest": lt.rate_of_interest,
            "tenure_months": min(lt.max_tenure_months, 12 if kind != "Emergency" else 6),
            "sanctioned_amount": {"Personal": 120000, "Housing": 600000, "Emergency": 30000}[kind],
            "first_deduction_month": get_first_day(nowdate()),
            "loan_account": lt.loan_account, "interest_account": lt.interest_account,
            "writeoff_account": lt.writeoff_account})
        loan.insert(ignore_permissions=True)
        loan.submit()
        loans += 1
    frappe.db.commit()
    print(f"{loans} sanctioned loans: ok")
    print("DONE. Log in as finance@test.com / treasury@test.com to test disbursement.")


