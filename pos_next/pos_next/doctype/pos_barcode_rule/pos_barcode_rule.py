# Copyright (c) 2026, BrainWise and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from pos_next.services.barcode_parser import MAX_BARCODE_LENGTH, MIN_BARCODE_LENGTH, rule_problems


class POSBarcodeRule(Document):
	def validate(self):
		self.prefix = (self.prefix or "").strip()
		problems = rule_problems(self.as_dict())
		if problems:
			frappe.throw("<br>".join(self._problem_message(code) for code in problems))

	def _problem_message(self, code):
		messages = {
			"barcode_type": _("Barcode Type must be Weighted or Priced."),
			"barcode_length": _("Barcode Length must be between {0} and {1}.").format(
				MIN_BARCODE_LENGTH, MAX_BARCODE_LENGTH
			),
			"prefix_missing": _("Prefix is required."),
			"prefix_too_long": _("Prefix must be shorter than the barcode."),
			"prefix_not_numeric": _("Prefix must be digits when the last digit is a check digit."),
			"item_code_out_of_range": _("Item Code Start and Length must fit inside the barcode."),
			"value_out_of_range": _("Value Start and Length must fit inside the barcode."),
			"segments_overlap": _("Prefix, item code, value and check digit must not overlap."),
			"value_decimals": _("Value Decimals can't be more than Value Length."),
		}
		return messages.get(code, code)
