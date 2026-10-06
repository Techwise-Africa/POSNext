/**
 * Weighted / priced barcode parser for POS Barcode Rule.
 *
 * Port of pos_next/services/barcode_parser.py so scans resolve offline. Both
 * are tested against pos_next/services/tests/barcode_cases.json. Change them
 * together.
 *
 * A rule describes a fixed-length barcode:
 *
 *     prefix | item code | value | check digit
 *     20       12345       01250   9            -> item 12345, 1.250 (Kg)
 *
 * Positions are 1-based. The value is digits with an implied decimal point
 * (`value_decimals`). Weighted rules read it as a quantity, Priced rules as
 * the line total.
 */

export const WEIGHTED = "Weighted";
export const PRICED = "Priced";
export const BARCODE_TYPES = [WEIGHTED, PRICED];

/** Marks results produced by POS Barcode Rule (vs the barcode_resolver app). */
export const SOURCE = "pos_next";

export const MIN_BARCODE_LENGTH = 4;
export const MAX_BARCODE_LENGTH = 30;

/** Keys a resolved scan adds to an item; never cache them with the item. */
export const RESOLVED_FIELDS = [
	"resolved_qty",
	"resolved_uom",
	"resolved_price",
	"resolved_barcode_type",
	"resolved_rate",
	"resolved_unit_rate",
	"resolved_conversion_factor",
	"resolved_amount",
	"resolved_rule",
];

const DIGITS = /^[0-9]+$/;

/**
 * A rule matched but the scan can't become a cart line.
 * `code` is stable (tests and messages key off it); `params` carries the
 * values a message needs.
 */
export class BarcodeResolutionError extends Error {
	constructor(code, params = {}) {
		super(code);
		this.name = "BarcodeResolutionError";
		this.code = code;
		this.params = params;
	}
}

function isDigits(value) {
	return typeof value === "string" && DIGITS.test(value);
}

function toInt(value) {
	const number = Number.parseInt(value, 10);
	return Number.isNaN(number) ? 0 : number;
}

function toFloat(value) {
	const number = Number.parseFloat(value);
	return Number.isNaN(number) ? 0 : number;
}

/** Round a non-negative number half-up, on its shortest decimal form. */
export function roundHalfUp(value, precision) {
	const factor = 10 ** precision;
	return Math.round(Number((value * factor).toPrecision(15))) / factor;
}

/** GS1 mod-10 check digit (EAN-8, EAN-13, UPC-A, GTIN-14) for `digits` without it. */
export function gs1CheckDigit(digits) {
	let total = 0;
	const reversed = [...digits].reverse();
	for (let index = 0; index < reversed.length; index++) {
		total += Number(reversed[index]) * (index % 2 === 0 ? 3 : 1);
	}
	return (10 - (total % 10)) % 10;
}

export function hasValidCheckDigit(barcode) {
	if (!isDigits(barcode) || barcode.length < 2) return false;
	return gs1CheckDigit(barcode.slice(0, -1)) === Number(barcode.slice(-1));
}

function segment(rule, name) {
	return [toInt(rule[`${name}_start`]), toInt(rule[`${name}_length`])];
}

/** Problem codes for a rule definition. An empty array means the rule is usable. */
export function ruleProblems(rule) {
	const problems = [];
	const length = toInt(rule.barcode_length);
	const prefix = rule.prefix || "";

	if (!BARCODE_TYPES.includes(rule.barcode_type)) problems.push("barcode_type");
	if (length < MIN_BARCODE_LENGTH || length > MAX_BARCODE_LENGTH) {
		problems.push("barcode_length");
		return problems;
	}

	if (!prefix) problems.push("prefix_missing");
	else if (prefix.length >= length) problems.push("prefix_too_long");
	if (rule.validate_check_digit && prefix && !isDigits(prefix)) {
		problems.push("prefix_not_numeric");
	}

	const ranges = prefix ? [[1, prefix.length]] : [];
	for (const name of ["item_code", "value"]) {
		const [start, size] = segment(rule, name);
		if (start < 1 || size < 1 || start + size - 1 > length) {
			problems.push(`${name}_out_of_range`);
		} else {
			ranges.push([start, start + size - 1]);
		}
	}
	if (rule.validate_check_digit) ranges.push([length, length]);
	const overlaps = ranges.some(([aStart, aEnd], i) =>
		ranges.slice(i + 1).some(([bStart, bEnd]) => aStart <= bEnd && bStart <= aEnd)
	);
	if (overlaps) problems.push("segments_overlap");

	const decimals = toInt(rule.value_decimals);
	if (decimals < 0 || decimals > segment(rule, "value")[1]) problems.push("value_decimals");

	return problems;
}

/** Parse `barcode` with one rule, or null when it doesn't fit the rule's layout. */
function matchRule(barcode, rule) {
	const prefix = rule.prefix || "";
	if (barcode.length !== toInt(rule.barcode_length) || !prefix || !barcode.startsWith(prefix)) {
		return null;
	}

	const [itemStart, itemLength] = segment(rule, "item_code");
	const [valueStart, valueLength] = segment(rule, "value");
	const itemBarcode = barcode.slice(itemStart - 1, itemStart - 1 + itemLength);
	const digits = barcode.slice(valueStart - 1, valueStart - 1 + valueLength);
	if (!itemBarcode || !isDigits(digits)) return null;

	const split = digits.length - toInt(rule.value_decimals);
	const integerValue = digits.slice(0, split).replace(/^0+/, "") || "0";
	const decimalValue = digits.slice(split);
	const value = Number(decimalValue ? `${integerValue}.${decimalValue}` : integerValue);

	const result = {
		source: SOURCE,
		rule: rule.name,
		barcode_type: rule.barcode_type,
		item_barcode: itemBarcode,
		integer_value: integerValue,
		decimal_value: decimalValue,
		uom: rule.uom || null,
		check_digit_valid: rule.validate_check_digit ? hasValidCheckDigit(barcode) : null,
	};
	result[rule.barcode_type === WEIGHTED ? "qty" : "price"] = value;
	return result;
}

/**
 * Parse `barcode` with the first rule (in order) whose layout it fits.
 *
 * Invalid rule definitions are skipped. A match whose check digit fails is
 * only returned when no later rule fits cleanly, with
 * `check_digit_valid: false` so the caller can report it.
 *
 * @param {string} barcode
 * @param {Object[]} rules - POS Barcode Rule definitions
 * @returns {Object|null} Same shape as the Python parse_barcode()
 */
export function parseBarcode(barcode, rules) {
	const code = String(barcode ?? "").trim();
	if (!code) return null;

	let badCheckDigit = null;
	for (const rule of rules || []) {
		if (!rule || ruleProblems(rule).length) continue;
		const match = matchRule(code, rule);
		if (!match) continue;
		if (match.check_digit_valid === false) {
			badCheckDigit = badCheckDigit || match;
			continue;
		}
		return match;
	}
	return badCheckDigit;
}

/**
 * Stock units in one `uom` of `item`, or null when the item has no conversion
 * for it. Looks at the item's own UOMs first, then at global UOM Conversion
 * Factors (`uomConversions` is `{from_uom: {to_uom: factor}}`).
 */
export function conversionFactor(item, uom, uomConversions = null) {
	const stockUom = item.stock_uom;
	if (!uom || uom === stockUom) return 1;
	for (const row of item.item_uoms || []) {
		if (row.uom === uom && toFloat(row.conversion_factor) > 0) {
			return toFloat(row.conversion_factor);
		}
	}
	if (uom === item.uom && toFloat(item.conversion_factor) > 0) {
		return toFloat(item.conversion_factor);
	}
	const factor = toFloat(uomConversions?.[uom]?.[stockUom]);
	return factor || null;
}

/**
 * Qty and rate for a priced label so that qty x rate rounds to the label
 * amount. Qty is rounded to the system float precision (as the server will).
 * When that rounding moves the line total off the label, the rate absorbs
 * the difference.
 */
export function pinPricedLine(amount, unitRate, qtyPrecision, currencyPrecision) {
	const qty = roundHalfUp(amount / unitRate, qtyPrecision);
	if (qty <= 0) throw new BarcodeResolutionError("value_too_small");
	let rate = unitRate;
	if (roundHalfUp(qty * rate, currencyPrecision) !== roundHalfUp(amount, currencyPrecision)) {
		rate = roundHalfUp(amount / qty, currencyPrecision);
	}
	return { qty, rate };
}

/**
 * Turn a parse result and item details into cart-line values.
 * Mirrors compute_resolved_line() in barcode_parser.py.
 *
 * @param {Object} parsed - parseBarcode() result
 * @param {Object} item - item_code, uom, stock_uom, price_list_rate/rate,
 *   uom_prices, item_uoms, conversion_factor
 * @param {Object} [options]
 * @param {Object} [options.uomConversions]
 * @param {number} [options.qtyPrecision=3]
 * @param {number} [options.currencyPrecision=2]
 * @returns {Object} resolved_* fields
 * @throws {BarcodeResolutionError}
 */
export function computeResolvedLine(
	parsed,
	item,
	{ uomConversions = null, qtyPrecision = 3, currencyPrecision = 2 } = {}
) {
	const barcodeType = parsed.barcode_type;
	if (!BARCODE_TYPES.includes(barcodeType)) {
		throw new BarcodeResolutionError("barcode_type", { barcode_type: barcodeType });
	}

	const itemCode = item.item_code;
	const itemUom = item.uom || item.stock_uom;
	const ruleUom = parsed.uom || itemUom;
	const uomPrices = item.uom_prices || {};
	const explicitRate = ruleUom ? toFloat(uomPrices[ruleUom]) : 0;
	let scale = 1;
	let lineUom;
	let unitRate;
	let lineFactor;

	if (explicitRate > 0) {
		// The item has its own price for the rule UOM: sell in that UOM.
		lineUom = ruleUom;
		unitRate = explicitRate;
		lineFactor = conversionFactor(item, ruleUom, uomConversions);
		if (lineFactor === null) {
			throw new BarcodeResolutionError("uom_not_convertible", { item_code: itemCode, uom: ruleUom });
		}
	} else {
		lineUom = itemUom;
		unitRate = toFloat(item.price_list_rate || item.rate);
		lineFactor = conversionFactor(item, itemUom, uomConversions) || 1;
		if (barcodeType === WEIGHTED && ruleUom !== itemUom) {
			const ruleFactor = conversionFactor(item, ruleUom, uomConversions);
			if (ruleFactor === null) {
				throw new BarcodeResolutionError("uom_not_convertible", { item_code: itemCode, uom: ruleUom });
			}
			scale = ruleFactor / lineFactor;
		}
	}

	if (unitRate <= 0) {
		throw new BarcodeResolutionError("missing_price", { item_code: itemCode, uom: lineUom });
	}

	const result = {
		resolved_uom: lineUom,
		resolved_barcode_type: barcodeType,
		resolved_unit_rate: unitRate,
		resolved_conversion_factor: lineFactor,
		resolved_rule: parsed.rule,
	};

	if (barcodeType === WEIGHTED) {
		const value = toFloat(parsed.qty);
		if (value <= 0) throw new BarcodeResolutionError("zero_value");
		const qty = roundHalfUp(value * scale, qtyPrecision);
		if (qty <= 0) throw new BarcodeResolutionError("value_too_small");
		Object.assign(result, { resolved_qty: qty, resolved_rate: unitRate, resolved_price: unitRate });
	} else {
		const amount = toFloat(parsed.price);
		if (amount <= 0) throw new BarcodeResolutionError("zero_value");
		const { qty, rate } = pinPricedLine(amount, unitRate, qtyPrecision, currencyPrecision);
		Object.assign(result, {
			resolved_qty: qty,
			resolved_rate: rate,
			resolved_price: amount,
			resolved_amount: amount,
		});
	}

	return result;
}

/** Copy of `item` without resolved_* scan data (safe to cache or reuse). */
export function stripResolvedFields(item) {
	if (!item || !RESOLVED_FIELDS.some((key) => key in item)) return item;
	const copy = { ...item };
	for (const key of RESOLVED_FIELDS) delete copy[key];
	return copy;
}
