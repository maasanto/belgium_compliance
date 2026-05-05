// Copyright (c) 2026, Antoine Maas and contributors
// For license information, please see license.txt

frappe.ui.form.on("Belgian VAT Declaration", {
	refresh(frm) {
		if (frm.doc.docstatus !== 2 && frm.doc.status !== "Draft") {
			frm.add_custom_button(
				__("Generate INTERVAT XML"),
				() => generate_xml(frm),
				__("Actions"),
			);
		}
		if (frm.doc.docstatus === 0 && frm.doc.status !== "Filed") {
			frm.add_custom_button(__("Compute"), () => compute(frm), __("Actions"));
		}
	},
});

function compute(frm) {
	frm.call("compute").then(() => {
		frm.reload_doc();
		frappe.show_alert(
			{ message: __("Declaration recomputed."), indicator: "green" },
			3,
		);
	});
}

function generate_xml(frm) {
	frm.call("generate_intervat_xml").then((r) => {
		if (!r.message) return;
		frm.reload_doc();
		const status = r.message.validation && r.message.validation.status;
		const indicator = status === "valid" ? "green" : status === "invalid" ? "red" : "orange";
		frappe.show_alert(
			{
				message: __("INTERVAT XML generated. XSD validation: {0}", [status || "unknown"]),
				indicator,
			},
			6,
		);
	});
}
