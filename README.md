# Employee Loan Lifecycle

An application for managing employee loans, including loan applications, approval workflows, loan disbursement, and repayment tracking.

## Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH

bench get-app $URL_OF_THIS_REPO --branch version-16

bench --site your-site.local install-app employee_loan_lifecycle
```

## Workflow Setup

This app includes two workflows:

* **Loan Application Workflow:** Manages employee loan approvals through Reporting Manager, HR Manager, Finance Manager, and CFO, depending on the approval band.
* **Loan Disbursement Workflow:** Manages finance verification, treasury release, and disbursement confirmation.

The workflows and the `custom_loan_recoveries` Salary Slip custom field are maintained as fixtures.

To export these configurations after making changes, configure the fixtures in `employee_loan_lifecycle/hooks.py` and run:

```bash
bench --site your-site.local export-fixtures
```

The exported JSON files are stored in:

```text
employee_loan_lifecycle/fixtures/
```

After pulling updated fixtures, run:

```bash
bench --site your-site.local migrate
bench --site your-site.local clear-cache
```

Make sure the required workflow roles and fields are available on the target site.

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/employee_loan_lifecycle

pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

* ruff
* eslint
* prettier
* pyupgrade

## CI

This app can use GitHub Actions for CI. The following workflows are configured:

* **CI:** Installs this app and runs unit tests on every push to the `develop` branch.
* **Linters:** Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.

## License

agpl-3.0
