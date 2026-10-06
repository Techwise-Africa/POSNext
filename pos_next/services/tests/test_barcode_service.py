# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

"""Integration tests for POS Barcode Rule scanning (search_by_barcode, get_items).

Run with:
    bench --site <site> run-tests --module pos_next.services.tests.test_barcode_service

Test data is prefixed with _PNXT_BC_ and rolled back by FrappeTestCase.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from pos_next.api.items import explain_barcode, get_barcode_rules, get_items, search_by_barcode
from pos_next.services.barcode import (
	BarcodeRuleError,
	is_barcode_resolver_available,
	resolve_barcode,
)
from pos_next.test_promotions import (
	_ensure_customer,
	_ensure_pos_profile,
	_resolve_company,
	_resolve_item_group,
	_resolve_mode_of_payment,
	_resolve_price_list,
	_resolve_warehouse,
)

PREFIX = "_PNXT_BC_"
SCALE_ITEM = f"{PREFIX}SCALE_ITEM"  # Item Barcode 98761, 4.50 / Kg
UNPRICED_ITEM = f"{PREFIX}UNPRICED"  # Item Barcode 98765, no price
WEIGHTED_RULE = f"{PREFIX}Weighted"  # 20 IIIII VVVVV C, 3 decimals, Kg
PRICED_RULE = f"{PREFIX}Priced"  # 22 IIIII VVVVV C, 2 decimals

WEIGHTED_SCAN = "2098761012507"  # item 98761, 1.250 Kg
WEIGHTED_BAD_CHECK = "2098761012508"
PRICED_SCAN = "2298761012990"  # item 98761, 12.99
UNKNOWN_ITEM_SCAN = "2097531012501"  # item 97531 doesn't exist
UNPRICED_SCAN = "2098765012503"  # item 98765 has no price
FULL_ITEM_BARCODE = "2011111012507"  # fits the weighted rule, but is an Item Barcode as a whole


def _ensure_uom(uom):
	if not frappe.db.exists("UOM", uom):
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert(ignore_permissions=True)


def _make_item(item_code, barcode, price_list, price=None):
	if not frappe.db.exists("Item", item_code):
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": _resolve_item_group(),
				"stock_uom": "Kg",
				"is_stock_item": 0,
				"is_sales_item": 1,
				"barcodes": [{"barcode": barcode}],
			}
		)
		item.flags.from_integration = True
		item.insert(ignore_permissions=True)
	if price is not None:
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"uom": "Kg",
				"price_list_rate": price,
			}
		).insert(ignore_permissions=True)


def _make_rule(name, **fields):
	if frappe.db.exists("POS Barcode Rule", name):
		frappe.delete_doc("POS Barcode Rule", name, force=True)
	doc = frappe.get_doc(
		{
			"doctype": "POS Barcode Rule",
			"rule_name": name,
			"barcode_length": 13,
			"item_code_start": 3,
			"item_code_length": 5,
			"value_start": 8,
			"value_length": 5,
			"validate_check_digit": 1,
			**fields,
		}
	)
	return doc.insert(ignore_permissions=True)


class TestBarcodeService(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		company = _resolve_company()
		warehouse = _resolve_warehouse(company)
		cls.price_list = _resolve_price_list(company)
		_ensure_customer()
		cls.pos_profile = _ensure_pos_profile(
			company, warehouse, cls.price_list, _resolve_mode_of_payment(company)
		)

		_ensure_uom("Kg")
		_make_item(SCALE_ITEM, "98761", cls.price_list, price=4.5)
		_make_item(UNPRICED_ITEM, "98765", cls.price_list)
		_make_rule(WEIGHTED_RULE, barcode_type="Weighted", prefix="20", value_decimals=3, uom="Kg")
		_make_rule(PRICED_RULE, barcode_type="Priced", prefix="22", value_decimals=2)

	def setUp(self):
		self.set_rules([WEIGHTED_RULE, PRICED_RULE])

	def set_rules(self, rules, disabled=()):
		name = frappe.db.get_value("POS Settings", {"pos_profile": self.pos_profile}, "name")
		settings = (
			frappe.get_doc("POS Settings", name)
			if name
			else frappe.get_doc({"doctype": "POS Settings", "pos_profile": self.pos_profile, "enabled": 1})
		)
		settings.set(
			"pos_barcode_rules",
			[{"barcode_rule": rule, "disable": int(rule in disabled)} for rule in rules],
		)
		settings.save(ignore_permissions=True)

	# --- Weighted / priced scans -------------------------------------------

	def test_weighted_scan(self):
		item = search_by_barcode(WEIGHTED_SCAN, self.pos_profile)
		self.assertEqual(item["item_code"], SCALE_ITEM)
		self.assertEqual(item["resolved_barcode_type"], "Weighted")
		self.assertAlmostEqual(item["resolved_qty"], 1.25)
		self.assertEqual(item["resolved_uom"], "Kg")
		self.assertAlmostEqual(item["resolved_rate"], 4.5)

	def test_priced_scan_line_total_matches_label(self):
		item = search_by_barcode(PRICED_SCAN, self.pos_profile)
		self.assertEqual(item["resolved_barcode_type"], "Priced")
		self.assertAlmostEqual(item["resolved_amount"], 12.99)
		self.assertAlmostEqual(round(item["resolved_qty"] * item["resolved_rate"], 2), 12.99)

	def test_resolve_barcode_keeps_result_shape(self):
		result = resolve_barcode(WEIGHTED_SCAN, self.pos_profile)
		for key in ("item_barcode", "integer_value", "decimal_value", "barcode_type", "uom", "qty"):
			self.assertIn(key, result)
		self.assertEqual(result["item_barcode"], "98761")
		self.assertEqual(result["rule"], WEIGHTED_RULE)

	def test_search_resolves_first_item(self):
		items = get_items(self.pos_profile, search_term=WEIGHTED_SCAN)
		self.assertTrue(items)
		self.assertEqual(items[0]["item_code"], SCALE_ITEM)
		self.assertAlmostEqual(items[0]["resolved_qty"], 1.25)

	# --- Errors ------------------------------------------------------------

	def test_bad_check_digit(self):
		with self.assertRaisesRegex(BarcodeRuleError, "check digit"):
			search_by_barcode(WEIGHTED_BAD_CHECK, self.pos_profile)
		self.assertIsNone(resolve_barcode(WEIGHTED_BAD_CHECK, self.pos_profile))

	def test_unknown_item(self):
		with self.assertRaisesRegex(BarcodeRuleError, "97531"):
			search_by_barcode(UNKNOWN_ITEM_SCAN, self.pos_profile)

	def test_missing_price(self):
		with self.assertRaisesRegex(BarcodeRuleError, UNPRICED_ITEM):
			search_by_barcode(UNPRICED_SCAN, self.pos_profile)

	def test_search_ignores_unsellable_scan(self):
		# Typing the barcode in the search box shouldn't fail, just not resolve.
		items = get_items(self.pos_profile, search_term=UNPRICED_SCAN)
		self.assertTrue(all("resolved_qty" not in item for item in items))

	def test_full_barcode_on_an_item_wins_over_a_missing_segment(self):
		# A store's own 20... barcode on an ordinary item must keep working.
		item = frappe.get_doc("Item", UNPRICED_ITEM)
		if not any(row.barcode == FULL_ITEM_BARCODE for row in item.barcodes):
			item.append("barcodes", {"barcode": FULL_ITEM_BARCODE})
			item.save(ignore_permissions=True)

		details = search_by_barcode(FULL_ITEM_BARCODE, self.pos_profile)
		self.assertEqual(details["item_code"], UNPRICED_ITEM)
		self.assertNotIn("resolved_qty", details)

	# --- Configuration -----------------------------------------------------

	def test_no_rules_leaves_scanning_unchanged(self):
		if is_barcode_resolver_available():
			self.skipTest("barcode_resolver is installed and may match")
		self.set_rules([])
		self.assertIsNone(resolve_barcode(WEIGHTED_SCAN, self.pos_profile))
		with self.assertRaises(frappe.ValidationError) as raised:
			search_by_barcode(WEIGHTED_BAD_CHECK, self.pos_profile)
		self.assertNotIsInstance(raised.exception, BarcodeRuleError)
		self.assertEqual(get_barcode_rules(self.pos_profile)["rules"], [])

	def test_disabled_rows_and_rules_are_ignored(self):
		self.set_rules([PRICED_RULE, WEIGHTED_RULE], disabled=[PRICED_RULE])
		self.assertEqual(
			[rule["name"] for rule in get_barcode_rules(self.pos_profile)["rules"]], [WEIGHTED_RULE]
		)

		frappe.db.set_value("POS Barcode Rule", WEIGHTED_RULE, "enabled", 0)
		self.addCleanup(frappe.db.set_value, "POS Barcode Rule", WEIGHTED_RULE, "enabled", 1)
		self.assertEqual(get_barcode_rules(self.pos_profile)["rules"], [])
		self.assertIsNone(resolve_barcode(WEIGHTED_SCAN, self.pos_profile))

	def test_rules_keep_table_order(self):
		self.set_rules([PRICED_RULE, WEIGHTED_RULE])
		self.assertEqual(
			[rule["name"] for rule in get_barcode_rules(self.pos_profile)["rules"]],
			[PRICED_RULE, WEIGHTED_RULE],
		)

	def test_duplicate_rule_rows_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.set_rules([WEIGHTED_RULE, WEIGHTED_RULE])

	def test_overlapping_rule_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			_make_rule(f"{PREFIX}Broken", barcode_type="Weighted", prefix="20", item_code_start=2)

	def test_explain_barcode(self):
		result = explain_barcode(WEIGHTED_SCAN, self.pos_profile)
		self.assertEqual(result["source"], "pos_next")
		self.assertTrue(result["parsed"]["check_digit_valid"])
		self.assertAlmostEqual(result["item"]["resolved_qty"], 1.25)
		self.assertIsNone(result["error"])

		result = explain_barcode(WEIGHTED_BAD_CHECK, self.pos_profile)
		self.assertFalse(result["parsed"]["check_digit_valid"])
		self.assertIn("check digit", result["error"])
