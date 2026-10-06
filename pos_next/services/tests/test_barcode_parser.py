# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Parser tests shared with POS/src/utils/__tests__/barcodeParser.spec.js.

barcode_parser has no Frappe imports, so these run under `bench run-tests`
or plain `python -m unittest pos_next.services.tests.test_barcode_parser`.
"""

import json
import unittest
from pathlib import Path

from pos_next.services.barcode_parser import (
	BarcodeResolutionError,
	compute_resolved_line,
	gs1_check_digit,
	has_valid_check_digit,
	parse_barcode,
	rule_problems,
)

CASES = json.loads((Path(__file__).parent / "barcode_cases.json").read_text(encoding="utf-8"))


def _rules(names):
	return [CASES["rules"][name] for name in names]


class TestBarcodeParser(unittest.TestCase):
	def assertSubset(self, actual, expected):
		"""Compare only the keys in `expected`; numbers within 1e-9."""
		self.assertIsNotNone(actual)
		for key, value in expected.items():
			if isinstance(value, int | float) and not isinstance(value, bool):
				self.assertAlmostEqual(actual[key], value, places=9, msg=key)
			else:
				self.assertEqual(actual[key], value, msg=key)

	def test_check_digit(self):
		for case in CASES["check_digit"]:
			with self.subTest(case["digits"]):
				self.assertEqual(gs1_check_digit(case["digits"]), case["check"])

	def test_valid_check_digit(self):
		for case in CASES["valid_check_digit"]:
			with self.subTest(case["barcode"]):
				self.assertEqual(has_valid_check_digit(case["barcode"]), case["valid"])

	def test_rule_problems(self):
		for case in CASES["rule_problems"]:
			with self.subTest(case["name"]):
				rule = {**CASES["rules"][case["rule"]], **case.get("override", {})}
				self.assertEqual(sorted(rule_problems(rule)), sorted(case["problems"]))

	def test_parse(self):
		for case in CASES["parse"]:
			with self.subTest(case["name"]):
				result = parse_barcode(case["barcode"], _rules(case["rules"]))
				if case["expected"] is None:
					self.assertIsNone(result)
				else:
					self.assertSubset(result, case["expected"])

	def test_compute(self):
		for case in CASES["compute"]:
			with self.subTest(case["name"]):
				kwargs = {
					"uom_conversions": case.get("uom_conversions"),
					"qty_precision": case.get("qty_precision", CASES["precision"]["qty"]),
					"currency_precision": CASES["precision"]["currency"],
				}
				item = CASES["items"][case["item"]]
				if "error" in case:
					with self.assertRaises(BarcodeResolutionError) as raised:
						compute_resolved_line(case["parsed"], item, **kwargs)
					self.assertEqual(raised.exception.code, case["error"]["code"])
					for key, value in case["error"].get("params", {}).items():
						self.assertEqual(raised.exception.params.get(key), value)
				else:
					self.assertSubset(compute_resolved_line(case["parsed"], item, **kwargs), case["expected"])
