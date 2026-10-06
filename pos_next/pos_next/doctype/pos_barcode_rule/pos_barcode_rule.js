// Copyright (c) 2026, BrainWise and contributors
// For license information, please see license.txt

const LAYOUT_FIELDS = [
	"barcode_type",
	"uom",
	"barcode_length",
	"prefix",
	"validate_check_digit",
	"item_code_start",
	"item_code_length",
	"value_start",
	"value_length",
	"value_decimals",
];

const SEGMENT_STYLES = {
	prefix: { label: __("Prefix"), color: "var(--gray-300)" },
	item_code: { label: __("Item Code"), color: "var(--blue-200)" },
	value: { label: __("Value"), color: "var(--green-200)" },
	check_digit: { label: __("Check Digit"), color: "var(--orange-200)" },
};

const handlers = { refresh: render_layout_preview };
for (const fieldname of LAYOUT_FIELDS) {
	handlers[fieldname] = render_layout_preview;
}
frappe.ui.form.on("POS Barcode Rule", handlers);

// GS1 mod-10 check digit (same as pos_next/services/barcode_parser.py)
function gs1_check_digit(digits) {
	let total = 0;
	[...digits].reverse().forEach((char, index) => {
		total += Number(char) * (index % 2 === 0 ? 3 : 1);
	});
	return (10 - (total % 10)) % 10;
}

function render_layout_preview(frm) {
	const doc = frm.doc;
	const length = cint(doc.barcode_length);
	const wrapper = frm.get_field("layout_preview").$wrapper;
	if (length < 1 || length > 30) {
		wrapper.html("");
		return;
	}

	// Which segment each position belongs to, and an example barcode.
	const owners = new Array(length).fill(null);
	const chars = new Array(length).fill("0");
	const prefix = doc.prefix || "";
	const place = (segment, start, size, text) => {
		for (let i = 0; i < size; i++) {
			const pos = start - 1 + i;
			if (pos >= 0 && pos < length) {
				owners[pos] = segment;
				chars[pos] = text[i] ?? "0";
			}
		}
	};
	place("prefix", 1, prefix.length, prefix);
	const itemLength = cint(doc.item_code_length);
	place("item_code", cint(doc.item_code_start), itemLength, "123456789012345".slice(0, itemLength));
	const valueLength = cint(doc.value_length);
	const valueDigits = "1250".padStart(valueLength, "0").slice(-valueLength);
	place("value", cint(doc.value_start), valueLength, valueDigits);
	if (doc.validate_check_digit) {
		owners[length - 1] = "check_digit";
		const body = chars.slice(0, -1).join("");
		chars[length - 1] = /^[0-9]+$/.test(body) ? String(gs1_check_digit(body)) : "?";
	}

	const cells = chars
		.map((char, index) => {
			const style = SEGMENT_STYLES[owners[index]];
			const background = style ? style.color : "transparent";
			return `<span title="${index + 1}" style="display:inline-block;width:1.6em;text-align:center;
				padding:4px 0;margin-right:1px;border-radius:3px;background:${background}">${frappe.utils.escape_html(
				char
			)}</span>`;
		})
		.join("");
	const legend = Object.values(SEGMENT_STYLES)
		.map(
			(style) => `<span style="margin-right:12px"><span style="display:inline-block;width:10px;
				height:10px;border-radius:2px;background:${style.color}"></span> ${style.label}</span>`
		)
		.join("");

	const itemStart = cint(doc.item_code_start) - 1;
	const itemCode = chars.slice(itemStart, itemStart + itemLength).join("");
	const split = Math.max(valueDigits.length - cint(doc.value_decimals), 0);
	const integerPart = valueDigits.slice(0, split).replace(/^0+/, "") || "0";
	const decimalPart = valueDigits.slice(split);
	const value = decimalPart ? `${integerPart}.${decimalPart}` : integerPart;
	const reading =
		doc.barcode_type === "Priced"
			? __("Example reads as item code {0} with a line total of {1}.", [itemCode, value])
			: __("Example reads as item code {0}, quantity {1} {2}.", [itemCode, value, doc.uom || ""]);

	wrapper.html(`
		<div style="font-family:var(--font-family-monospace, monospace);font-size:1.1em">${cells}</div>
		<div class="text-muted small" style="margin-top:8px">${legend}</div>
		<div class="text-muted small" style="margin-top:4px">${frappe.utils.escape_html(reading)}</div>
	`);
}
