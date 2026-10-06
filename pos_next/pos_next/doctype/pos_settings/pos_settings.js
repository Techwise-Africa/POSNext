// Copyright (c) 2024, BrainWise and contributors
// For license information, please see license.txt

frappe.ui.form.on("POS Settings", {
	refresh(frm) {
		// Set query for loyalty program filtered by POS Profile company
		frm.set_query("default_loyalty_program", function () {
			if (!frm.doc.__company) {
				return { filters: {} };
			}
			return {
				filters: {
					company: frm.doc.__company,
				},
			};
		});

		// Fetch company when form loads
		if (frm.doc.pos_profile) {
			fetch_pos_profile_company(frm);
		}
	},

	pos_profile(frm) {
		// Clear loyalty program when POS Profile changes
		frm.set_value("default_loyalty_program", "");
		frm.doc.__company = null;

		if (frm.doc.pos_profile) {
			fetch_pos_profile_company(frm);
		}
	},

	test_barcode(frm) {
		if (frm.is_new() || frm.is_dirty()) {
			frappe.msgprint(__("Save POS Settings first. The test uses the saved barcode rules."));
			return;
		}
		show_test_barcode_dialog(frm);
	},
});

function show_test_barcode_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Test Barcode"),
		fields: [
			{
				fieldname: "barcode",
				fieldtype: "Data",
				label: __("Barcode"),
				description: __("Scan or type a barcode, then press Enter."),
				reqd: 1,
			},
			{ fieldname: "result", fieldtype: "HTML" },
		],
		primary_action_label: __("Test"),
		primary_action(values) {
			frappe
				.call({
					method: "pos_next.pos_next.doctype.pos_settings.pos_settings.test_barcode",
					args: { pos_profile: frm.doc.pos_profile, barcode: values.barcode },
				})
				.then((r) => {
					dialog.fields_dict.result.$wrapper.html(render_test_barcode_result(r.message));
					dialog.fields_dict.barcode.$input.trigger("select");
				});
		},
	});

	// Scanners end with Enter
	dialog.fields_dict.barcode.$input.on("keydown", (e) => {
		if (e.key === "Enter") {
			e.preventDefault();
			dialog.primary_action(dialog.get_values());
		}
	});
	dialog.show();
}

function render_test_barcode_result(result) {
	if (!result) return "";
	const esc = (value) => frappe.utils.escape_html(value == null ? "" : String(value));
	const row = (label, value) => `<tr><th style="width:40%">${esc(label)}</th><td>${value}</td></tr>`;
	const rows = [];
	const parsed = result.parsed;

	const sources = {
		pos_next: __("POS Barcode Rule {0}", [parsed ? esc(parsed.rule) : ""]),
		barcode_resolver: __("barcode_resolver app"),
	};
	rows.push(row(__("Read by"), result.source ? sources[result.source] : __("Plain barcode (no rule matched)")));

	if (parsed) {
		const checkDigit =
			parsed.check_digit_valid === null
				? __("Not checked")
				: parsed.check_digit_valid
					? `<span class="indicator-pill green">${esc(__("Valid"))}</span>`
					: `<span class="indicator-pill red">${esc(__("Invalid"))}</span>`;
		rows.push(row(__("Check Digit"), checkDigit));
		rows.push(row(__("Item Code in Barcode"), esc(parsed.item_barcode)));
		rows.push(
			parsed.barcode_type === "Priced"
				? row(__("Encoded Price"), esc(parsed.price))
				: row(__("Encoded Quantity"), `${esc(parsed.qty)} ${esc(parsed.uom || "")}`)
		);
	}

	const item = result.item;
	if (item) {
		rows.push(row(__("Item"), `${esc(item.item_code)}: ${esc(item.item_name)}`));
		if (item.resolved_barcode_type) {
			rows.push(row(__("Cart Quantity"), `${esc(item.resolved_qty)} ${esc(item.resolved_uom)}`));
			// barcode_resolver puts the unit rate (Weighted) or the label total (Priced) in resolved_price
			const priced = item.resolved_barcode_type === "Priced";
			const rate = item.resolved_rate ?? (priced ? null : item.resolved_price);
			const total = rate != null ? item.resolved_qty * rate : item.resolved_price;
			if (rate != null) rows.push(row(__("Rate"), esc(format_currency(rate))));
			rows.push(row(__("Line Total"), esc(format_currency(total))));
		} else {
			rows.push(row(__("Rate"), `${esc(format_currency(item.price_list_rate))} / ${esc(item.uom)}`));
		}
	}

	const error = result.error
		? `<div class="alert alert-danger" style="margin-top:8px">${esc(result.error)}</div>`
		: "";
	return `<table class="table table-bordered table-sm" style="margin-top:8px">${rows.join("")}</table>${error}`;
}

function fetch_pos_profile_company(frm) {
	frappe.db.get_value("POS Profile", frm.doc.pos_profile, "company", (r) => {
		if (r && r.company) {
			frm.doc.__company = r.company;
		}
	});
}
