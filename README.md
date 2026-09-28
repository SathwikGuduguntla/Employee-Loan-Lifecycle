# Employee Loan Lifecycle

A Frappe application for managing employee loans, including loan applications, approval workflows, disbursement verification, and repayment tracking.

## 1. Requirements

* Frappe Framework
* ERPNext
* Employee records and required employee roles
* The `employee_loan_lifecycle` custom app

The app uses Frappe Workflows and fixtures to version-control its workflow configuration.

## 2. Workflows

This app currently uses two workflows:

### Loan Application Workflow

Controls the approval process for employee loan applications.

| State                     | Allowed role      | Docstatus |
| ------------------------- | ----------------- | --------: |
| Draft                     | Employee          |         0 |
| Pending Reporting Manager | Reporting Manager |         1 |
| Pending HR                | HR Manager        |         1 |
| Pending Finance           | Finance Manager   |         1 |
| Pending CFO               | CFO               |         1 |
| Approved                  | System Manager    |         1 |
| Rejected                  | System Manager    |         1 |
| Returned                  | Employee          |         0 |

The approval path depends on the application's `approval_band`:

* **B1:** Reporting Manager → HR Manager → Approved
* **B2:** Reporting Manager → HR Manager → Finance Manager → Approved
* **B3/B4:** Reporting Manager → HR Manager → Finance Manager → CFO → Approved

The workflow uses the `workflow_state` field.

### Loan Disbursement Workflow

Controls finance verification and treasury release of approved loan disbursements.

| State             | Allowed role     | Docstatus |
| ----------------- | ---------------- | --------: |
| Draft             | Finance Manager  |         0 |
| Finance Verified  | Finance Manager  |         0 |
| Treasury Released | Treasury Manager |         0 |
| Disbursed         | System Manager   |         1 |
| Rejected          | System Manager   |         0 |
| Returned          | Finance Manager  |         0 |

Transitions:

1. Finance Manager: Draft → Finance Verified (**Verify**)
2. Treasury Manager: Finance Verified → Treasury Released (**Release**)
3. Treasury Manager: Treasury Released → Disbursed (**Confirm Disbursed**)
4. Finance Manager can reject a Draft or Finance Verified disbursement.
5. Finance Manager can return a Finance Verified disbursement.
6. Finance Manager can resubmit a Returned disbursement to Draft.

The workflow uses the `status` field.

## 3. Configure Fixtures

The workflow definitions and the Salary Slip custom field are maintained as fixtures.

Open:

`employee_loan_lifecycle/hooks.py`

Configure the fixtures:

```python
fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            ["dt", "=", "Salary Slip"],
            ["fieldname", "=", "custom_loan_recoveries"],
        ],
    },
    {
        "dt": "Workflow",
        "filters": [
            [
                "name",
                "in",
                [
                    "Loan Application Workflow",
                    "Loan Disbursement Workflow",
                ],
            ],
        ],
    },
]
```

If the app already has a `fixtures` list, add these entries to it rather than creating a second list.

## 4. Export Fixtures

After creating or modifying the workflows or the custom field in the Frappe UI, export the fixtures.

From the bench directory:

```bash
bench --site project.local export-fixtures
```

Replace `project.local` with your target site name.

Frappe exports the selected documents into:

```text
employee_loan_lifecycle/
└── employee_loan_lifecycle/
    └── fixtures/
```

The directory should contain JSON files for the selected Custom Field and Workflow documents.

Check the exported files:

```bash
ls -l apps/employee_loan_lifecycle/employee_loan_lifecycle/fixtures/
```

Review the JSON files before committing them.

## 5. Install and Set Up

### Get the app

If the app is not already present in your bench:

```bash
bench get-app employee_loan_lifecycle <GIT_REPOSITORY_URL>
```

Use the actual repository URL and branch for your app.

### Install the app

```bash
bench --site project.local install-app employee_loan_lifecycle
```

### Import fixtures and migrate

After configuring the app and fixtures, run:

```bash
bench --site project.local migrate
bench --site project.local clear-cache
```

If the app is already installed, migration is the normal step after pulling changes that include updated fixtures.

## 6. Required Roles and Fields

Before using the workflows, verify that the site has the roles referenced by the workflow:

* Employee
* Reporting Manager
* HR Manager
* Finance Manager
* Treasury Manager
* CFO
* System Manager

Verify that the relevant DocTypes contain the workflow state fields:

* Loan Application: `workflow_state`
* Loan Disbursement: `status`

The Loan Application workflow also uses `approval_band` in its transition conditions. Ensure that this field exists and is populated correctly before testing approval transitions.

The Salary Slip custom field fixture is filtered by:

* DocType: `Salary Slip`
* Fieldname: `custom_loan_recoveries`

## 7. Assign Roles to Users

In the Frappe Desk:

1. Open **User**.
2. Select the user who will perform the workflow action.
3. Assign the required role.
4. Save the user.
5. Repeat for the other approvers and treasury users.

Assign roles according to your organization's approval and segregation-of-duties requirements. Avoid giving one user multiple approval roles if your process requires different people to perform sequential approvals.

## 8. Verify the Workflows

After installation or migration:

1. Open **Workflow** in Frappe Desk.
2. Confirm that **Loan Application Workflow** is active.
3. Confirm that **Loan Disbursement Workflow** is active.
4. Check each workflow's states, transitions, allowed roles, and conditions.
5. Create a test Loan Application and check that the expected actions appear for the appropriate role.
6. Test the disbursement workflow separately using a valid Loan Disbursement record.

Test each approval band and the rejection and return paths. Confirm that users cannot perform transitions outside their permitted roles.

## 9. Update and Commit Changes

Whenever a workflow is changed in the UI:

1. Update the workflow on the development site.

2. Export the fixtures:

   ```bash
   bench --site project.local export-fixtures
   ```

3. Review the changed JSON files.

4. Commit the fixtures and any related app code:

   ```bash
   cd apps/employee_loan_lifecycle
   git status
   git add employee_loan_lifecycle/hooks.py employee_loan_lifecycle/fixtures
   git commit -m "Update loan workflow fixtures"
   ```

5. Push the commit to your repository:

   ```bash
   git push
   ```

On another site, pull the app changes and run migration:

```bash
bench --site project.local migrate
bench --site project.local clear-cache
```

## 10. Important Notes

* Export fixtures from the development site after every intended configuration change.
* Keep workflow names consistent with the names in the fixture filters.
* Confirm that required roles and custom fields exist on the target site.
* Test workflow conditions and role permissions after importing.
* Fixtures version-control configuration; they do not replace application logic, server-side validation, or testing.
* Keep approval and disbursement responsibilities appropriately separated.
