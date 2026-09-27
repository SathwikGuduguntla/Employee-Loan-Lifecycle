// Buttons are convenience only; all checks are server-side.
frappe.ui.form.on("Loan Disbursement", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.docstatus === 0 && frm.doc.status === "Draft") {
			frm.add_custom_button(__("Finance Verify"), () => run(frm, "finance_verify"));
		}

		if (frm.doc.docstatus === 0 && frm.doc.status === "Finance Verified") {
			frm.add_custom_button(__("Treasury Release"), () => {
				frappe.prompt(
					[
						{ fieldname: "reference_no", fieldtype: "Data", label: __("Bank Reference No"), reqd: 1 },
						{ fieldname: "reference_date", fieldtype: "Date", label: __("Reference Date"), reqd: 1, default: frm.doc.disbursement_date },
					],
					(values) => run(frm, "treasury_release", values),
					__("Release Funds")
				);
			});
		}

		if (frm.doc.docstatus === 1 && frm.doc.status === "Treasury Released") {
			frm.add_custom_button(__("Mark Disbursed"), () => run(frm, "mark_disbursed"));
		}
	},
});

function run(frm, method, args) {
	frm.call({ doc: frm.doc, method, args, freeze: true, callback: () => frm.reload_doc() });
}