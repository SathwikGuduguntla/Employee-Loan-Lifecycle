frappe.ui.form.on("Loan Application", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1 || frm.doc.status !== "Pending Approval") {
			return;
		}

		frm.add_custom_button(__("Approve"), () => take_action(frm, "Approved"));
		frm.add_custom_button(__("Return"), () => take_action(frm, "Returned"));
		frm.add_custom_button(__("Reject"), () => take_action(frm, "Rejected"));
	},
});

function take_action(frm, action) {
	const needs_comment = action !== "Approved";

	const run = (comment) => {
		frappe.call({
			method:
				"employee_loan_lifecycle.employee_loan_lifecycle.doctype.loan_application.loan_application.take_action",
			args: {
				name: frm.doc.name,
				action: action,
				comment: comment || null,
			},
			freeze: true,
			freeze_message: __("Processing {0}...", [action]),
			callback: (r) => {
				if (!r.exc) {
					frm.reload_doc();
				}
			},
		});
	};

	if (needs_comment) {
		frappe.prompt(
			[
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label: __("Comment"),
					reqd: 1,
				},
			],
			(values) => run(values.comment),
			__(action)
		);
	} else {
		run(null);
	}
}