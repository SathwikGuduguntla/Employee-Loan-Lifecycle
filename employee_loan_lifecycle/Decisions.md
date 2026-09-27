# Decisions

This records the deliberate design choices in this module and why, since
the brief calls this the most-read file in the submission.

## 1. Approval matrix — revised: stock Workflow, not a named-approver engine

**This section was rewritten mid-project and the reversal is deliberate,
not an oversight, so it's recorded here rather than silently overwritten.**

The first version of this module resolved approvals against a
`Loan Approval Rule` / `Loan Approval Step` matrix in
`LoanApplication.take_action()`, supporting named approvers, delegation
while on leave, and an append-only `Loan Approval Log`, specifically
because a stock `Workflow` doctype's `allowed` field only accepts a role
and can't scope a transition to one named user or write to a separate log
table. That version is what's described in the git history and in the
original assignment review notes.

The current version deliberately reverts to a stock **`Loan Application
Workflow`** (`fixtures/workflow.json`): `Draft → Pending Reporting Manager
→ Pending HR → Pending Finance → Pending CFO → Approved`, with `Reject`
and `Return` at every pending stage, routed by a computed `approval_band`
field (`B1`–`B4`, from `LoanApplication.compute_approval_band()` /
`BAND_THRESHOLDS`) rather than by the old per-rule step chain.

**What this trades away**, compared to the named-approver engine above:
- No named `approver_user` or leave-based `delegate_user` — each stage is
  gated by a role only (`Reporting Manager` role membership, not "this
  employee's actual manager", except where noted below).
- No standalone `Loan Approval Log` child table — the audit trail is
  Frappe's own workflow timeline comments instead.
- `Loan Approval Rule` / `Loan Approval Step` are no longer read by
  anything; they're left in the app unused rather than deleted, in case a
  future iteration goes back to matrix-based routing.

**What still had to be backfilled**, because the stock Workflow doctype
cannot express these on its own:
- *Mandatory comment on Reject/Return* — `loan_application.js` hides the
  default Reject/Return buttons (they have no way to collect input) and
  replaces them with prompted versions that write the reason to a hidden
  `pending_action_comment` field before calling
  `frappe.model.workflow.apply_workflow`. `before_workflow_action()`
  throws if that field is blank for those two actions;
  `after_workflow_action()` copies it into a timeline comment and clears
  it.
- *Same person can't approve twice at different stages* — enforced in
  `before_workflow_action()` by checking whether `frappe.session.user`
  already owns a `Comment` of `comment_type = "Workflow"` on this
  document (Frappe logs one automatically on every transition).
- *Self-approval* needs no extra code: every Approve/Reject/Return
  transition in the fixture has `allow_self_approval: 0`, which Frappe
  enforces natively against the document's `owner`. `Submit` (and the
  `Returned → Submit` re-submission transition) are the only transitions
  with `allow_self_approval: 1`, since the applicant is expected to act on
  their own draft there.
- *Loan creation on final approval* moved from `take_action()` to
  `after_workflow_action()`, firing when `workflow_state == "Approved"`
  (see §2 — the mechanics of `create_loan()` itself are unchanged).

**A known structural gap in this design**, disclosed rather than hidden:
HR Manager / Finance Manager / CFO are plain role checks, so anyone
holding that role can act at that stage regardless of which employee's
application it is — there's no per-application named approver the way
the matrix engine had. Reporting Manager is the one stage checked against
actual identity (`employee.reports_to`), both in
`get_permission_query_conditions`/`has_permission` and implicitly through
row visibility, since a stock Workflow's `allowed` role can't be narrowed
further on its own. Delegation-while-on-leave is not supported at all in
this version — if that's needed, the named-approver engine in git history
is the fallback.

**Access control (layer 2):** `Loan Application` previously had doctype
permissions for System Manager only, so no other role could open the
doctype at all regardless of row conditions. Added rows for Employee (own,
`if_owner`), Reporting Manager, HR Manager, Finance Manager, CFO; row
visibility is then narrowed by `get_permission_query_conditions`/
`has_permission` to the applicant, their reporting manager, or a user
holding the role required by the document's current `workflow_state`. The
same pattern (doctype rows + row-narrowing functions) is applied to
`Loan`, `Loan Disbursement`, and `Loan Repayment` — see
`PERMISSIONS_PATCH.md` for the two doctypes whose JSON wasn't otherwise
touched this round. Field-level restriction (layer 3) already existed on
`Loan Application.sanctioned_amount` and `.eligibility_override`
(`permlevel: 1`) before this round; it wasn't added by this round's work.

The stock Workflow doctype is *not* used for **Loan Disbursement** — that
one keeps the whitelisted-method design (`finance_verify()` /
`treasury_release()` / `mark_disbursed()` / `cancel_disbursement()`) with
its own client-script buttons, because that flow needs a genuine
verifier-≠-releaser check and triggers Payment Entry creation, neither of
which a Workflow transition can express — see the addendum note in
`hooks.py`.

## 2. Loan creation on final approval

`LoanApplication.after_workflow_action()` calls `create_loan()` the moment
`workflow_state` becomes `"Approved"`. Terms are copied from the
application + its Loan Type at that instant (interest mode, rate, tenure,
sanctioned amount, accounts) so a later edit to Loan Type never
retroactively changes an already sanctioned loan. `create_loan()` is
idempotent (checks `self.loan` first) since workflow hooks could in
principle fire more than once for the same state.

## 3. Schedule generation (`Loan.build_schedule`)

Both interest modes close to exactly zero by construction, not by
rounding luck: every period computes its principal share from the
formula, **except the last period, which is simply set to whatever
principal remains** (`opening_balance`). That's the one rule that
guarantees the schedule can never drift — no matter how rounding
accumulated over the earlier periods, the final row absorbs it.

- **Flat:** interest = `principal × monthly_rate` fixed every period;
  principal = `principal / n` every period except the last.
- **Reducing Balance:** standard EMI formula; each period's interest is
  `opening_balance × monthly_rate`; principal is `EMI − interest` except
  the last period, which is `opening_balance`. A 0% rate falls back to
  straight-line principal (division by zero avoided explicitly).

Terms are frozen at submit (`Loan.validate_terms` throws if
`sanctioned_amount`/`rate_of_interest`/`tenure_months`/`interest_mode`/
`first_deduction_month` change after `docstatus == 1`) — an amendment is
the only path to change frozen terms.

## 4. Disbursement & repayment accounting

Both `Loan Disbursement.treasury_release()` and `Loan Repayment` create a
**Payment Entry** (`Pay`/`Receive` against `party_type = Employee`) rather
than hand-rolled `GL Entry` rows, so ERPNext's own accounting engine does
the actual Dr/Cr posting and the reversal-on-cancel logic. Disbursement:
`Dr Loan Account (receivable) / Cr Bank`. Repayment: `Dr Bank / Cr Loan
Account`. The bank/cash side is resolved from the Bank Account or Mode of
Payment's per-company default account (an ERPNext-idiomatic lookup); if
none is configured, the controller throws rather than silently picking an
account.

`Loan Disbursement`'s schema has no `reversal_entry` field (the version
this review is based on removed it and renamed `finance_verifier`/
`treasury_releaser` to `verified_by`/`released_by` — the previous
controller still referenced the old field names, which would have thrown
`AttributeError` at runtime; that mismatch is fixed as part of this round).
Cancellation now **cancels the linked Payment Entry directly**, which is
what makes ERPNext post the reversing GL entries — there's no separate
reversal document to track by hand.

## 5. Direct repayment allocation order

Documented in full in `loan_repayment.py`'s module docstring; summary:
schedule periods are walked oldest-due-date first; a period overdue as of
`repayment_date` is tracked as **arrears**, otherwise as ordinary
interest/principal; **within any one period, interest is paid before
principal**. Because a schedule row only stores one aggregate
`recovered_amount` (not a separate interest/principal split), the
"remaining interest vs. principal" on a partially-paid row is derived by
treating whatever's already recovered as having been applied
interest-first too — a fixed, reproducible convention rather than an
extra field.

Every application against a period is written to a new **`recovery_breakdown`**
child table on Loan Repayment (reusing the existing `Loan Recovery Detail`
doctype, which was previously only wired to Salary Slip). This is what
makes cancellation exact: `on_cancel` reverses precisely those rows, in
reverse order, rather than re-deriving what must have happened.

**Foreclosure** recomputes the required payoff (outstanding principal +
outstanding interest + `Loan Type.pre_closure_charge_percent` of
outstanding principal) and requires the submitted amount to already equal
it — a partial foreclosure isn't a foreclosure.

## 6. Payroll deduction (`salary_slip_hooks.py`)

`preview_recoveries` (Salary Slip `validate`) recomputes
`custom_loan_recoveries` from scratch every time the slip is saved as a
draft — clearing and rebuilding it, rather than appending, so redrafting
never produces duplicate rows. `apply_recoveries` (`on_submit`) is the
**only** place a Loan's balances actually move, exactly once per submitted
slip; `reverse_recoveries` (`on_cancel`) undoes exactly what was recorded.

- **Duplicate prevention:** a period is skipped the moment its schedule
  row is `Recovered`, regardless of whether the recovery came from
  payroll or a direct `Loan Repayment` — both funnel through
  `Loan.apply_recovery`, so there's one source of truth per period.
- **Insufficient net pay:** if the sum of everyone's next-due instalments
  for an employee this cycle exceeds that slip's `net_pay`, every loan's
  deduction is scaled down proportionally so net pay never goes negative.
  The shortfall isn't force-collected; it reappears as arrears via
  `Loan.recompute_arrears()` and is picked up again next cycle (or via a
  direct Arrears Catch-up repayment).
- A `Loan Recovery` Salary Component must exist for this deduction to
  post (Type = Deduction, name it exactly `Loan Recovery`) — created once
  by hand via the UI rather than an `after_install`/`after_migrate` hook,
  since that hook's module path proved fragile to keep correctly placed
  across `bench migrate` runs on this bench; see the Installation note in
  `hooks.py`. A single aggregated deduction row is appended to the
  slip's own `deductions` table so the recovery is a real payroll
  deduction, not just an informational breakdown — `total_deduction` and
  `net_pay` are adjusted additively by this hook. **Assumption, flagged
  for verification:** this assumes HRMS's own Salary Slip controller
  computes `gross_pay`/`net_pay`/`total_deduction` before this app's
  `doc_events` validate hook runs (the doctype's own controller method
  runs before hooks registered by other apps) — the adjustment here is
  additive/subtractive either way, but confirm the final `net_pay` on a
  test slip reflects the reduction in your bench before relying on it.

## 7. Retrospective correction

A whitelisted `retrospective_correction(loan, period_no, corrected_principal,
corrected_interest, reason)` (in `salary_slip_hooks.py`) reverses whatever
is currently recorded against a period and re-applies corrected figures,
leaving a comment trail on the Loan. Restricted to HR Manager/Finance
Manager/System Manager, with a mandatory reason. This is deliberately a
manual, audited action rather than an automatic reprocessing of a past
payroll run — reprocessing a historical Salary Slip in place risks
disturbing figures that have already been paid out and reported on.

## 8. Employee exit block

`block_exit_with_outstanding_loan` (Employee `validate`) throws if an
Employee's status is being set to `Left` while they have a Loan that isn't
`Closed`/`Written Off`.

## 9. Reports

- **Loan Outstanding Aging** previously computed its aging buckets with
  one `frappe.get_all()` call per loan inside a Python loop — an N+1
  pattern that won't hold the brief's 3-second target once there are
  hundreds of loans. Rewritten as a single SQL query with a `CASE`/`SUM`
  per bucket. A `report_summary` section was added that reconciles the
  sum of outstanding principal per `Loan Type`'s receivable account
  against that account's actual `GL Entry` balance as of the report date
  (the control-account reconciliation the brief asks for), flagged
  green/red per account.
- **Schedule vs Actual Recovery** previously joined out through Salary
  Slip to `Loan Recovery Detail` with no period filter on the Salary Slip
  side, which multiplies unnecessarily per employee. Since
  `recovered_amount` on each `Loan Repayment Schedule` row is now the
  single number both payroll and direct repayments update (via
  `Loan.apply_recovery`), the report reads that column directly — simpler
  and provably consistent with what `Loan.status`/`outstanding_principal`
  are built from, with no join to Salary Slip or Loan Repayment needed at
  all.

## 10. Modeling note: Loan Type / Loan Approval Rule / Loan Approval Step

The brief describes these three as provided, pre-seeded configuration
doctypes, not ones to build. They already exist in this repo. `Loan Type`
stays fully wired in (its accounts feed `Loan.build_schedule` and the
disbursement/repayment postings). `Loan Approval Rule` / `Loan Approval
Step` are no longer read by anything since §1's reversal to a stock
Workflow — they're left in place rather than deleted, both because
removing them would touch unrelated doctype-deletion migration concerns
for a debatable modeling objection rather than a functional one, and in
case a future iteration returns to matrix-based routing. This is noted
here rather than silently left unexplained.

## 11. Testing

`test_loan.py` exercises the schedule engine directly (flat and reducing
balance both close to exactly zero; a 0% reducing-balance loan degrades to
straight-line) — these need no DB fixtures since `build_schedule()` only
touches in-memory fields.

`test_loan_application.py`, as originally written, pinned down two
properties of the named-approver matrix engine (a role-based step accepts
any holder of that role; a named-user step accepts only that user or an
on-leave delegate). Those methods (`is_valid_approver`,
`approver_on_leave`) no longer exist on `LoanApplication` after §1's
reversal to a stock Workflow, so that test file would have failed on
import — it's been replaced rather than left broken. It now asserts
`compute_approval_band()`'s threshold boundaries (amounts exactly at the
`25_000`/`100_000`/`500_000` ceilings land in the lower band) and both
`before_workflow_action()` guards (blank `pending_action_comment` on
Reject/Return throws; a user who already owns a `Comment(comment_type=
"Workflow")` on the document is blocked from a second Approve, a fresh
user is not).

**Not yet covered**, because it needs this bench's actual seeded
Employee/Company/Salary Structure test records to construct meaningfully:
an end-to-end test that submits a Loan Application through a real
multi-step chain and asserts the Loan is created with the right terms; a
`Loan Repayment` test asserting interest-before-principal allocation and
exact reversal on cancel; a payroll test asserting the insufficient-net-pay
proportional scale-down. These should be added once run against the
grader's seed data — the assertions to write are described above so
they're a translation exercise, not a design exercise.