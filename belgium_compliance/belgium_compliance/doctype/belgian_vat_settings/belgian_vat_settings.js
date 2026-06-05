// Copyright (c) 2026, Antoine Maas and contributors
// For license information, please see license.txt

frappe.ui.form.on("Belgian VAT Settings", {
	refresh(frm) {
		if (frm.is_new()) return;
		add_materialise_buttons(frm);
		show_materialisation_status(frm);
	},
});

function add_materialise_buttons(frm) {
	frm.add_custom_button(
		__("Preview (dry-run)"),
		() => run_materialise(frm, true),
		__("Tax Templates"),
	);

	frm.add_custom_button(
		__("Materialise tax templates"),
		() => {
			frappe.confirm(
				__(
					"This will create the missing Belgian VAT tax templates and link them to {0}. Existing links are kept untouched. Continue?",
					[frm.doc.company.bold()],
				),
				() => run_materialise(frm, false),
			);
		},
		__("Tax Templates"),
	);
}

function run_materialise(frm, dry_run) {
	frappe.call({
		method: "belgium_compliance.setup.materialise_for_company",
		args: { company: frm.doc.company, dry_run: dry_run ? 1 : 0 },
		freeze: true,
		freeze_message: dry_run
			? __("Computing preview…")
			: __("Materialising tax templates…"),
		callback: (r) => {
			const result = r.message;
			if (!result) return;

			const label = dry_run ? __("Preview") : __("Materialised");
			frappe.show_alert(
				{
					message: __("{0}: {1} created, {2} already present, {3} errors", [
						label,
						result.created,
						result.skipped,
						result.errors,
					]),
					indicator: result.errors ? "orange" : "green",
				},
				8,
			);

			if (result.errors && result.error_details && result.error_details.length) {
				const rows = result.error_details
					.slice(0, 10)
					.map(
						(e) =>
							`<tr><td>${frappe.utils.escape_html(e.definition)}</td>` +
							`<td>${frappe.utils.escape_html(e.error)}</td></tr>`,
					)
					.join("");
				frappe.msgprint({
					title: __("Materialisation errors"),
					indicator: "red",
					message: `<table class="table table-condensed">
						<thead><tr><th>${__("Tax Definition")}</th><th>${__("Error")}</th></tr></thead>
						<tbody>${rows}</tbody>
					</table>`,
				});
			}

			if (!dry_run) {
				show_materialisation_status(frm);
			}
		},
	});
}

function show_materialisation_status(frm) {
	const company = frm.doc.company;
	if (!company) return;

	Promise.all([
		frappe.db.count("Belgian VAT Tax Definition"),
		frappe.db.get_list("Belgian VAT Tax Template Link", {
			filters: { company },
			fields: ["tax_definition"],
			limit: 0,
		}),
	]).then(([total, links]) => {
		const linked_definitions = new Set(links.map((l) => l.tax_definition));
		const linked = linked_definitions.size;
		const pct = total ? Math.round((linked / total) * 100) : 0;
		const colour = pct === 100 ? "green" : pct > 0 ? "blue" : "grey";
		frm.dashboard.add_indicator(
			__("Materialised: {0} / {1} definitions ({2}%)", [linked, total, pct]),
			colour,
		);
	});
}
