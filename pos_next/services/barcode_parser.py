"""
Weighted / priced barcode parser for POS Barcode Rule.

Pure Python with no Frappe imports. The same logic is ported to
POS/src/utils/barcodeParser.js so scans resolve offline; both are tested
against pos_next/services/tests/barcode_cases.json. Change them together.

A rule describes a fixed-length barcode:

    prefix | item code | value | check digit
    20       12345       01250   9            -> item 12345, 1.250 (Kg)

Positions are 1-based. The value is digits with an implied decimal point
(`value_decimals`). Weighted rules read it as a quantity, Priced rules as the
line total.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

WEIGHTED = "Weighted"
PRICED = "Priced"
BARCODE_TYPES = (WEIGHTED, PRICED)

# Marks results produced by POS Barcode Rule (vs the barcode_resolver app).
SOURCE = "pos_next"

MIN_BARCODE_LENGTH = 4
MAX_BARCODE_LENGTH = 30

_DIGITS = re.compile(r"^[0-9]+$")


class BarcodeResolutionError(Exception):
	"""A rule matched but the scan can't become a cart line.

	`code` is stable (tests and translated messages key off it); `params`
	carries the values a message needs.
	"""

	def __init__(self, code: str, **params):
		super().__init__(code)
		self.code = code
		self.params = params


def _is_digits(value) -> bool:
	return bool(value) and bool(_DIGITS.match(value))


def _int(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


def _flt(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def round_half_up(value: float, precision: int) -> float:
	"""Round a non-negative number half-up, on its shortest decimal form."""
	return float(Decimal(repr(float(value))).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP))


def gs1_check_digit(digits: str) -> int:
	"""GS1 mod-10 check digit (EAN-8, EAN-13, UPC-A, GTIN-14) for `digits` without it."""
	total = 0
	for index, char in enumerate(reversed(digits)):
		total += int(char) * (3 if index % 2 == 0 else 1)
	return (10 - total % 10) % 10


def has_valid_check_digit(barcode: str) -> bool:
	if not _is_digits(barcode) or len(barcode) < 2:
		return False
	return gs1_check_digit(barcode[:-1]) == int(barcode[-1])


def _segment(rule: dict, name: str) -> tuple[int, int]:
	return _int(rule.get(f"{name}_start")), _int(rule.get(f"{name}_length"))


def rule_problems(rule: dict) -> list[str]:
	"""Problem codes for a rule definition. An empty list means the rule is usable."""
	problems = []
	length = _int(rule.get("barcode_length"))
	prefix = rule.get("prefix") or ""

	if rule.get("barcode_type") not in BARCODE_TYPES:
		problems.append("barcode_type")
	if not MIN_BARCODE_LENGTH <= length <= MAX_BARCODE_LENGTH:
		problems.append("barcode_length")
		return problems

	if not prefix:
		problems.append("prefix_missing")
	elif len(prefix) >= length:
		problems.append("prefix_too_long")
	if rule.get("validate_check_digit") and prefix and not _is_digits(prefix):
		problems.append("prefix_not_numeric")

	ranges = [(1, len(prefix))] if prefix else []
	for name in ("item_code", "value"):
		start, size = _segment(rule, name)
		if start < 1 or size < 1 or start + size - 1 > length:
			problems.append(f"{name}_out_of_range")
		else:
			ranges.append((start, start + size - 1))
	if rule.get("validate_check_digit"):
		ranges.append((length, length))
	overlaps = any(
		a_start <= b_end and b_start <= a_end
		for i, (a_start, a_end) in enumerate(ranges)
		for b_start, b_end in ranges[i + 1 :]
	)
	if overlaps:
		problems.append("segments_overlap")

	decimals = _int(rule.get("value_decimals"))
	if decimals < 0 or decimals > _segment(rule, "value")[1]:
		problems.append("value_decimals")

	return problems


def _match_rule(barcode: str, rule: dict) -> dict | None:
	"""Parse `barcode` with one rule, or None when it doesn't fit the rule's layout."""
	prefix = rule.get("prefix") or ""
	if len(barcode) != _int(rule.get("barcode_length")) or not prefix or not barcode.startswith(prefix):
		return None

	item_start, item_length = _segment(rule, "item_code")
	value_start, value_length = _segment(rule, "value")
	item_barcode = barcode[item_start - 1 : item_start - 1 + item_length]
	digits = barcode[value_start - 1 : value_start - 1 + value_length]
	if not item_barcode or not _is_digits(digits):
		return None

	decimals = _int(rule.get("value_decimals"))
	split = len(digits) - decimals
	integer_value = digits[:split].lstrip("0") or "0"
	decimal_value = digits[split:]
	value = float(f"{integer_value}.{decimal_value}" if decimal_value else integer_value)

	barcode_type = rule.get("barcode_type")
	result = {
		"source": SOURCE,
		"rule": rule.get("name"),
		"barcode_type": barcode_type,
		"item_barcode": item_barcode,
		"integer_value": integer_value,
		"decimal_value": decimal_value,
		"uom": rule.get("uom") or None,
		"check_digit_valid": has_valid_check_digit(barcode) if rule.get("validate_check_digit") else None,
	}
	result["qty" if barcode_type == WEIGHTED else "price"] = value
	return result


def parse_barcode(barcode: str, rules: list[dict]) -> dict | None:
	"""Parse `barcode` with the first rule (in order) whose layout it fits.

	Invalid rule definitions are skipped. A match whose check digit fails is
	only returned when no later rule fits cleanly, with
	`check_digit_valid=False` so the caller can report it.

	Returns a dict shaped like barcode_resolver's result (item_barcode,
	integer_value, decimal_value, barcode_type, uom, qty | price) plus
	source, rule and check_digit_valid; or None.
	"""
	barcode = (barcode or "").strip()
	if not barcode:
		return None

	bad_check_digit = None
	for rule in rules or []:
		if rule_problems(rule):
			continue
		match = _match_rule(barcode, rule)
		if not match:
			continue
		if match["check_digit_valid"] is False:
			bad_check_digit = bad_check_digit or match
			continue
		return match
	return bad_check_digit


def conversion_factor(item: dict, uom: str | None, uom_conversions: dict | None = None) -> float | None:
	"""Stock units in one `uom` of `item`, or None when the item has no conversion for it.

	Looks at the item's own UOMs first, then at global UOM Conversion Factors
	(`uom_conversions` is `{from_uom: {to_uom: factor}}`).
	"""
	stock_uom = item.get("stock_uom")
	if not uom or uom == stock_uom:
		return 1.0
	for row in item.get("item_uoms") or []:
		if row.get("uom") == uom and _flt(row.get("conversion_factor")) > 0:
			return _flt(row.get("conversion_factor"))
	if uom == item.get("uom") and _flt(item.get("conversion_factor")) > 0:
		return _flt(item.get("conversion_factor"))
	factor = _flt(((uom_conversions or {}).get(uom) or {}).get(stock_uom))
	return factor or None


def pin_priced_line(amount: float, unit_rate: float, qty_precision: int, currency_precision: int):
	"""Qty and rate for a priced label so that qty x rate rounds to the label amount.

	Qty is rounded to the system float precision (as the server will). When
	that rounding moves the line total off the label, the rate absorbs the
	difference.
	"""
	qty = round_half_up(amount / unit_rate, qty_precision)
	if qty <= 0:
		raise BarcodeResolutionError("value_too_small")
	rate = unit_rate
	if round_half_up(qty * rate, currency_precision) != round_half_up(amount, currency_precision):
		rate = round_half_up(amount / qty, currency_precision)
	return qty, rate


def compute_resolved_line(
	parsed: dict,
	item: dict,
	uom_conversions: dict | None = None,
	qty_precision: int = 3,
	currency_precision: int = 2,
) -> dict:
	"""Turn a parse result and item details into cart-line values.

	`item` needs item_code, uom, stock_uom, price_list_rate (or rate) and
	optionally uom_prices, item_uoms and conversion_factor.

	Weighted: qty is the encoded value, converted from the rule UOM to the
	item's UOM unless the item has its own price for the rule UOM.
	Priced: qty = encoded total / unit price, pinned so the line total
	matches the label.

	Keeps barcode_resolver's keys (resolved_qty, resolved_uom,
	resolved_price, resolved_barcode_type) and adds resolved_rate (line
	rate), resolved_unit_rate, resolved_conversion_factor, resolved_rule and,
	for Priced, resolved_amount.

	Raises BarcodeResolutionError for missing prices, UOMs the item can't be
	sold in, and zero values.
	"""
	barcode_type = parsed.get("barcode_type")
	if barcode_type not in BARCODE_TYPES:
		raise BarcodeResolutionError("barcode_type", barcode_type=barcode_type)

	item_code = item.get("item_code")
	item_uom = item.get("uom") or item.get("stock_uom")
	rule_uom = parsed.get("uom") or item_uom
	uom_prices = item.get("uom_prices") or {}
	explicit_rate = _flt(uom_prices.get(rule_uom)) if rule_uom else 0.0
	scale = 1.0

	if explicit_rate > 0:
		# The item has its own price for the rule UOM: sell in that UOM.
		line_uom, unit_rate = rule_uom, explicit_rate
		line_factor = conversion_factor(item, rule_uom, uom_conversions)
		if line_factor is None:
			raise BarcodeResolutionError("uom_not_convertible", item_code=item_code, uom=rule_uom)
	else:
		line_uom, unit_rate = item_uom, _flt(item.get("price_list_rate") or item.get("rate"))
		line_factor = conversion_factor(item, item_uom, uom_conversions) or 1.0
		if barcode_type == WEIGHTED and rule_uom != item_uom:
			rule_factor = conversion_factor(item, rule_uom, uom_conversions)
			if rule_factor is None:
				raise BarcodeResolutionError("uom_not_convertible", item_code=item_code, uom=rule_uom)
			scale = rule_factor / line_factor

	if unit_rate <= 0:
		raise BarcodeResolutionError("missing_price", item_code=item_code, uom=line_uom)

	result = {
		"resolved_uom": line_uom,
		"resolved_barcode_type": barcode_type,
		"resolved_unit_rate": unit_rate,
		"resolved_conversion_factor": line_factor,
		"resolved_rule": parsed.get("rule"),
	}

	if barcode_type == WEIGHTED:
		value = _flt(parsed.get("qty"))
		if value <= 0:
			raise BarcodeResolutionError("zero_value")
		qty = round_half_up(value * scale, qty_precision)
		if qty <= 0:
			raise BarcodeResolutionError("value_too_small")
		result.update(resolved_qty=qty, resolved_rate=unit_rate, resolved_price=unit_rate)
	else:
		amount = _flt(parsed.get("price"))
		if amount <= 0:
			raise BarcodeResolutionError("zero_value")
		qty, rate = pin_priced_line(amount, unit_rate, qty_precision, currency_precision)
		result.update(resolved_qty=qty, resolved_rate=rate, resolved_price=amount, resolved_amount=amount)

	return result
