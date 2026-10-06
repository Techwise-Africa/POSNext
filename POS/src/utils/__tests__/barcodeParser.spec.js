import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
	BarcodeResolutionError,
	computeResolvedLine,
	gs1CheckDigit,
	hasValidCheckDigit,
	parseBarcode,
	ruleProblems,
	stripResolvedFields,
} from "../barcodeParser";

// Shared with pos_next/services/tests/test_barcode_parser.py
const CASES = JSON.parse(
	readFileSync(
		new URL("../../../../pos_next/services/tests/barcode_cases.json", import.meta.url),
		"utf8"
	)
);

const rulesFor = (names) => names.map((name) => CASES.rules[name]);

/** Compare only the keys in `expected`; numbers within 1e-9. */
function expectSubset(actual, expected) {
	expect(actual).not.toBeNull();
	for (const [key, value] of Object.entries(expected)) {
		if (typeof value === "number") {
			expect(actual[key], key).toBeCloseTo(value, 9);
		} else {
			expect(actual[key], key).toEqual(value);
		}
	}
}

describe("barcodeParser (shared cases)", () => {
	it.each(CASES.check_digit)("check digit of $digits is $check", ({ digits, check }) => {
		expect(gs1CheckDigit(digits)).toBe(check);
	});

	it.each(CASES.valid_check_digit)("hasValidCheckDigit($barcode) is $valid", ({ barcode, valid }) => {
		expect(hasValidCheckDigit(barcode)).toBe(valid);
	});

	it.each(CASES.rule_problems)("rule problems: $name", ({ rule, override, problems }) => {
		const definition = { ...CASES.rules[rule], ...(override || {}) };
		expect([...ruleProblems(definition)].sort()).toEqual([...problems].sort());
	});

	it.each(CASES.parse)("parse: $name", ({ barcode, rules, expected }) => {
		const result = parseBarcode(barcode, rulesFor(rules));
		if (expected === null) {
			expect(result).toBeNull();
		} else {
			expectSubset(result, expected);
		}
	});

	it.each(CASES.compute)("compute: $name", (testCase) => {
		const run = () =>
			computeResolvedLine(testCase.parsed, CASES.items[testCase.item], {
				uomConversions: testCase.uom_conversions || null,
				qtyPrecision: testCase.qty_precision ?? CASES.precision.qty,
				currencyPrecision: CASES.precision.currency,
			});

		if (testCase.error) {
			let error = null;
			try {
				run();
			} catch (e) {
				error = e;
			}
			expect(error).toBeInstanceOf(BarcodeResolutionError);
			expect(error.code).toBe(testCase.error.code);
			expect(error.params).toMatchObject(testCase.error.params || {});
		} else {
			expectSubset(run(), testCase.expected);
		}
	});
});

describe("barcodeParser", () => {
	it("turns the README barcode into a 1.250 Kg line", () => {
		const parsed = parseBarcode("2012345012509", rulesFor(["weighted_kg"]));
		const line = computeResolvedLine(parsed, CASES.items.banana);
		expect(line.resolved_qty).toBe(1.25);
		expect(line.resolved_qty * line.resolved_rate).toBeCloseTo(5.625, 9);
	});

	it("does not mutate the item it computes from", () => {
		const item = structuredClone(CASES.items.banana);
		computeResolvedLine({ barcode_type: "Priced", price: 9, rule: "Priced" }, item);
		expect(item).toEqual(CASES.items.banana);
	});

	it("strips resolved scan data before an item is cached", () => {
		const item = { item_code: "BANANA", rate: 4.5, resolved_qty: 1.25, resolved_rate: 4.5 };
		expect(stripResolvedFields(item)).toEqual({ item_code: "BANANA", rate: 4.5 });
		expect(item.resolved_qty).toBe(1.25);

		const plain = { item_code: "BANANA" };
		expect(stripResolvedFields(plain)).toBe(plain);
	});
});
