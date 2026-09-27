// Copyright (c) 2026, sathwik and contributors
// For license information, please see license.txt

frappe.ui.form.on("Loan Disbursement", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}

		// The server (finance_verify / treasury_release / mark_disbursed /
		// cancel_disbursement) is the actual authority on role, status and
		// segregation-of-duty checks — these buttons are shown to anyone
		// with write access and the call will throw if this user isn't
		// the right person for this step.
		if (frm.doc.status === "Draft") {
			frm.add_custom_button(__("Finance Verify"), () => call(frm, "finance_verify"));
		}

		if (frm.doc.status === "Finance Verified") {
			frm.add_custom_button(__("Treasury Release"), () => call(frm, "treasury_release"));
		}

		if (frm.doc.status === "Treasury Released") {
			frm.add_custom_button(__("Mark Disbursed"), () => call(frm, "mark_disbursed"));
		}

		if (["Finance Verified", "Treasury Released", "Disbursed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Cancel Disbursement"), () => cancel(frm));
		}
	},
});

function call(frm, method) {
	frm.call({
		doc: frm.doc,
		method,
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}

function cancel(frm) {
	frappe.prompt(
		{
			fieldname: "reason",
			fieldtype: "Small Text",
			label: __("Cancellation Reason"),
			reqd: 1,
		},
		(values) => {
			frm.call({
				doc: frm.doc,
				method: "cancel_disbursement",
				args: { reason: values.reason },
				freeze: true,
				callback: () => frm.reload_doc(),
			});
		},
		__("Cancel Disbursement")
	);
}