"""
Weighted / priced barcode service for POS Next.

Barcodes are read with the POS Barcode Rules enabled for the POS Profile in
POS Settings (see barcode_parser.py). When none of them match and the
optional barcode_resolver app is installed, its rules are tried next. With
no POS Barcode Rules configured, behaviour is exactly the barcode_resolver
integration (or nothing when that app isn't installed).

Usage:
    from pos_next.services import resolve_barcode, compute_resolved_item_data

    result = resolve_barcode("2012345012509", pos_profile)
    if result:
        print(result["item_barcode"], result.get("qty"))
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TypedDict

import frappe
from erpnext.stock.get_item_details import get_conversion_factor
from frappe import _
from frappe.utils import cint, flt

from pos_next.services.barcode_parser import (
	SOURCE as NATIVE_SOURCE,
)
from pos_next.services.barcode_parser import (
	BarcodeResolutionError,
	compute_resolved_line,
	parse_barcode,
)

logger = logging.getLogger(__name__)

# Fields of POS Barcode Rule the parser (Python and JS) reads.
BARCODE_RULE_FIELDS = [
	"name",
	"barcode_type",
	"barcode_length",
	"prefix",
	"item_code_start",
	"item_code_length",
	"value_start",
	"value_length",
	"value_decimals",
	"uom",
	"validate_check_digit",
]


class BarcodeRuleError(frappe.ValidationError):
	"""A POS Barcode Rule matched but the scan can't be added to the cart.

	The POS shows its message instead of the generic "item not found".
	"""


class BarcodeResult(TypedDict, total=False):
	"""Type definition for barcode resolution result."""

	item_barcode: str  # The barcode from Item Barcodes table
	integer_value: str  # Integer part of the encoded value
	decimal_value: str  # Decimal part of the encoded value
	barcode_type: str  # "Weighted" or "Priced"
	uom: str | None  # UOM from Item Barcodes table
	qty: float | None  # Quantity (only for weighted barcodes)
	price: float | None  # Encoded total (only for priced barcodes)
	source: str  # "pos_next" when a POS Barcode Rule matched
	rule: str  # POS Barcode Rule name (POS Barcode Rule matches only)
	check_digit_valid: bool | None  # None when the rule doesn't check it


class ResolvedItemData(TypedDict, total=False):
	"""Type definition for resolved item data to be applied to cart."""

	resolved_qty: float | None
	resolved_uom: str | None
	resolved_price: float | None
	resolved_barcode_type: str | None
	# Added for POS Barcode Rule matches:
	resolved_rate: float  # Line rate (pinned for priced labels)
	resolved_unit_rate: float  # Item price for resolved_uom
	resolved_conversion_factor: float  # Conversion factor of resolved_uom
	resolved_amount: float  # Label total (priced only)
	resolved_rule: str


def is_native_result(resolved_barcode) -> bool:
	"""Whether a resolve_barcode() result came from a POS Barcode Rule."""
	return bool(resolved_barcode) and resolved_barcode.get("source") == NATIVE_SOURCE


def get_pos_barcode_rules(pos_profile: str) -> list[dict]:
	"""Enabled POS Barcode Rule definitions for a POS Profile, in POS Settings table order."""
	settings_name = frappe.db.get_value("POS Settings", {"pos_profile": pos_profile}, "name")
	if not settings_name:
		return []

	settings_doc = frappe.get_cached_doc("POS Settings", settings_name)
	names = []
	for row in settings_doc.get("pos_barcode_rules") or []:
		if row.barcode_rule and not row.disable and row.barcode_rule not in names:
			names.append(row.barcode_rule)
	if not names:
		return []

	rows = frappe.get_all(
		"POS Barcode Rule",
		filters={"name": ["in", names], "enabled": 1},
		fields=BARCODE_RULE_FIELDS,
	)
	by_name = {row.name: dict(row) for row in rows}
	return [by_name[name] for name in names if name in by_name]


def get_uom_conversions(uoms) -> dict[str, dict[str, float]]:
	"""Global UOM Conversion Factors touching `uoms`, as {from_uom: {to_uom: factor}}.

	Both directions are included, so a rule in Gram can sell an item stocked
	in Kg without a Gram row on the item.
	"""
	uoms = sorted({uom for uom in uoms if uom})
	if not uoms:
		return {}

	fields = ["from_uom", "to_uom", "value"]
	conversions: dict[str, dict[str, float]] = {}
	for row in frappe.get_all("UOM Conversion Factor", filters={"from_uom": ["in", uoms]}, fields=fields):
		if flt(row.value) > 0:
			conversions.setdefault(row.from_uom, {})[row.to_uom] = flt(row.value)
	for row in frappe.get_all("UOM Conversion Factor", filters={"to_uom": ["in", uoms]}, fields=fields):
		if flt(row.value) > 0:
			conversions.setdefault(row.to_uom, {}).setdefault(row.from_uom, 1 / flt(row.value))
	return conversions


def get_precisions() -> tuple[int, int]:
	"""(qty, currency) precision, from the same System Settings the POS bootstrap sends."""
	settings = (
		frappe.db.get_value("System Settings", None, ["float_precision", "currency_precision"], as_dict=True)
		or {}
	)
	return cint(settings.get("float_precision")) or 3, cint(settings.get("currency_precision")) or 2


def match_native_barcode(barcode: str, pos_profile: str) -> BarcodeResult | None:
	"""Parse with the profile's POS Barcode Rules, including check-digit failures."""
	rules = get_pos_barcode_rules(pos_profile)
	if not rules:
		return None
	return parse_barcode(barcode, rules)


def barcode_error_message(code: str, params: dict) -> str:
	"""Translated message for a BarcodeResolutionError code."""
	if code == "invalid_check_digit":
		return _("Invalid check digit in barcode {0}. Rescan the label.").format(params.get("barcode"))
	if code == "item_not_found":
		return _("No item found for code {0} in barcode {1} (rule {2}).").format(
			params.get("item_barcode"), params.get("barcode"), params.get("rule")
		)
	if code == "missing_price":
		return _("Item {0} has no selling price for UOM {1}.").format(
			params.get("item_code"), params.get("uom")
		)
	if code == "uom_not_convertible":
		return _("Item {0} has no conversion factor for UOM {1}.").format(
			params.get("item_code"), params.get("uom")
		)
	if code == "zero_value":
		return _("Barcode {0} encodes a zero weight or price.").format(params.get("barcode"))
	if code == "value_too_small":
		return _("The weight or price in barcode {0} is too small to sell.").format(params.get("barcode"))
	return _("Barcode {0} could not be read ({1}).").format(params.get("barcode"), code)


def throw_barcode_error(error: BarcodeResolutionError, barcode: str):
	"""Raise BarcodeRuleError with the message for a resolution failure."""
	frappe.throw(
		barcode_error_message(error.code, {"barcode": barcode, **error.params}),
		exc=BarcodeRuleError,
		title=_("Barcode Not Added"),
	)


@lru_cache(maxsize=1)
def is_barcode_resolver_available() -> bool:
	"""
	Check if the barcode_resolver app is installed.

	Returns:
	    bool: True if barcode_resolver is available, False otherwise.

	Note:
	    Result is cached for performance. Server restart clears the cache.
	"""
	return "barcode_resolver" in frappe.get_installed_apps()


def resolve_barcode(barcode: str, pos_profile: str) -> BarcodeResult | None:
	"""
	Resolve a weighted/priced barcode.

	The POS Profile's POS Barcode Rules are tried first. When none matches
	(or none is configured), the barcode_resolver app is used if installed.
	A POS Barcode Rule match with a bad check digit is not returned; see
	match_native_barcode() to report it.

	Args:
	    barcode: The barcode string to resolve.
	    pos_profile: POS Profile whose rules apply.

	Returns:
	    BarcodeResult dict if the barcode matches a rule, None otherwise.

	Example:
	    >>> result = resolve_barcode("2012345012509", "Main POS")
	    >>> if result:
	    ...     print(f"Item: {result['item_barcode']}, Qty: {result['qty']}")
	"""
	native = match_native_barcode(barcode, pos_profile)
	if native and native.get("check_digit_valid") is not False:
		logger.info(
			"resolve_barcode: barcode=%r matched POS Barcode Rule %r -> item_barcode=%s",
			barcode,
			native.get("rule"),
			native.get("item_barcode"),
		)
		return native

	if not is_barcode_resolver_available():
		logger.debug("resolve_barcode: barcode_resolver app not installed")
		return None

	try:
		from barcode_resolver.barcode_resolver.doctype.barcode_rule.utils import (
			resolve_barcode as _resolve_barcode,
		)
	except ImportError:
		logger.warning("resolve_barcode: ImportError loading upstream; clearing cache")
		is_barcode_resolver_available.cache_clear()
		return None

	barcode_rules = _get_barcode_rules_for_profile(pos_profile)
	logger.info(
		"resolve_barcode: barcode=%r profile=%r rules=%s",
		barcode,
		pos_profile,
		"ALL_ACTIVE" if barcode_rules is None else barcode_rules,
	)

	try:
		result = _resolve_barcode(barcode, barcode_rules)
	except Exception:
		frappe.log_error(
			title="Barcode Resolver Error",
			message=f"Error resolving barcode {barcode!r} for profile {pos_profile!r}\n\n{frappe.get_traceback()}",
		)
		logger.exception("resolve_barcode: upstream raised for barcode=%r", barcode)
		return None

	if result is None:
		logger.info("resolve_barcode: no match for barcode=%r", barcode)
	else:
		logger.info(
			"resolve_barcode: matched barcode=%r -> item_code=%s qty=%s price=%s type=%s",
			barcode,
			result.get("item_code"),
			result.get("qty"),
			result.get("price"),
			result.get("barcode_type"),
		)
	return result


def _get_barcode_rules_for_profile(pos_profile: str) -> list[str] | None:
	"""Return enabled Barcode Rule names for the given POS Profile.

	Returns None when no per-profile configuration exists, which signals
	the resolver to consider every active Barcode Rule. This keeps the
	resolver functional on sites that have not yet migrated to the
	POS Next `POS Settings` doctype (which adds `pos_profile` +
	`barcode_rules`).
	"""
	settings_name = frappe.db.get_value("POS Settings", {"pos_profile": pos_profile}, "name")
	if not settings_name:
		logger.info(
			"resolve_barcode: no POS Settings row for profile=%r — falling back to all active rules",
			pos_profile,
		)
		return None

	try:
		settings_doc = frappe.get_cached_doc("POS Settings", settings_name)
	except Exception:
		logger.warning(
			"resolve_barcode: could not load POS Settings %r — falling back",
			settings_name,
		)
		return None

	rules_table = getattr(settings_doc, "barcode_rules", None) or []
	enabled = [row.barcode_rule for row in rules_table if not row.disable]
	logger.debug(
		"resolve_barcode: profile=%r settings=%r enabled_rules=%s",
		pos_profile,
		settings_name,
		enabled,
	)
	return enabled


def _coerce_value(resolved_barcode, field: str) -> float | None:
	"""Pull a numeric value out of the resolver result regardless of upstream shape.

	Newer barcode_resolver versions populate `qty` / `price` directly. Older
	versions only return `integer_value` and `decimal_value` segments which
	must be joined as `"<int>.<dec>"`. Supporting both lets us keep working
	across mixed upstream versions in production.
	"""
	direct = resolved_barcode.get(field)
	if direct is not None:
		try:
			return float(direct)
		except (TypeError, ValueError):
			pass

	if field == "qty" and resolved_barcode.get("barcode_type") != "Weighted":
		return None
	if field == "price" and resolved_barcode.get("barcode_type") != "Priced":
		return None

	integer_value = resolved_barcode.get("integer_value")
	decimal_value = resolved_barcode.get("decimal_value")
	if integer_value is None and decimal_value is None:
		return None
	try:
		return float(f"{integer_value or '0'}.{decimal_value or '0'}")
	except ValueError:
		return None


def compute_resolved_item_data(
	resolved_barcode: BarcodeResult | None,
	item,
) -> ResolvedItemData | None:
	"""
	Compute qty and uom from resolved barcode data.

	For weighted barcodes: uses qty directly from the barcode.
	For priced barcodes: computes qty = encoded_price / item_rate.

	Args:
	    resolved_barcode: The result from resolve_barcode().
	    item: Item details (item_code, uom, rate/price_list_rate, uom_prices, ...).

	Returns:
	    ResolvedItemData with resolved_qty, resolved_uom, and resolved_barcode_type,
	    or None if no valid resolution.

	Raises:
	    BarcodeResolutionError: for POS Barcode Rule matches that can't be sold
	    (missing price, unknown UOM, zero value). barcode_resolver results
	    never raise it.

	Example:
	    >>> resolved = resolve_barcode("2012345012509", "Main POS")
	    >>> if resolved:
	    ...     item_data = compute_resolved_item_data(resolved, item=item_details)
	    ...     print(f"Qty: {item_data['resolved_qty']}, UOM: {item_data['resolved_uom']}")
	"""
	if is_native_result(resolved_barcode):
		qty_precision, currency_precision = get_precisions()
		return compute_resolved_line(
			resolved_barcode,
			item,
			uom_conversions=get_uom_conversions([resolved_barcode.get("uom")]),
			qty_precision=qty_precision,
			currency_precision=currency_precision,
		)

	if not resolved_barcode or not is_barcode_resolver_available():
		return None

	from barcode_resolver.barcode_resolver.doctype.barcode_rule.utils import BarcodeTypes

	barcode_type = resolved_barcode.get("barcode_type")
	barcode_uom = resolved_barcode.get("uom")
	# If barcode resolver didn't provide a UOM, fall back to item's stock UOM
	if not barcode_uom:
		barcode_uom = item.get("uom")
	uom_prices = item.get("uom_prices", {})
	barcode_uom_price = uom_prices.get(barcode_uom)
	item_uom = item.get("uom")
	item_price = item.get("rate")
	item_name = item.get("item_code")
	if item_name is None:
		frappe.log_error(
			title="Barcode Resolver Error",
			message=f"Item code is missing in item data: {item}",
		)
		return None

	# Older barcode_resolver versions return integer_value/decimal_value segments;
	# newer versions return parsed qty/price directly. Support both by preferring
	# the parsed value and falling back to reconstructing from segments.
	encoded_qty = _coerce_value(resolved_barcode, "qty")
	encoded_price = _coerce_value(resolved_barcode, "price")
	if barcode_type == BarcodeTypes.WEIGHTED.value:
		if encoded_qty is None:
			logger.warning(
				"compute_resolved_item_data: weighted barcode missing qty and segments: %s",
				resolved_barcode,
			)
			return None
		qty = float(encoded_qty)
		uom = barcode_uom
		price = barcode_uom_price
		if barcode_uom not in uom_prices:
			conversion_factor = get_conversion_factor(item_name, barcode_uom).get("conversion_factor", 1)
			qty *= conversion_factor
			uom = item_uom
			price = item_price

		return {
			"resolved_qty": qty,
			"resolved_uom": uom,
			"resolved_price": price,
			"resolved_barcode_type": barcode_type,
		}
	elif barcode_type == BarcodeTypes.PRICED.value:
		if encoded_price is None:
			logger.warning(
				"compute_resolved_item_data: priced barcode missing price and segments: %s",
				resolved_barcode,
			)
			return None
		encoded_price = float(encoded_price)
		if barcode_uom in uom_prices:
			barcode_uom_price = uom_prices.get(barcode_uom)
			price = barcode_uom_price
			uom = barcode_uom
			qty = encoded_price / price if price and price > 0 else None
		else:
			conversion_factor = get_conversion_factor(item_name, barcode_uom).get("conversion_factor", 1)
			uom = barcode_uom
			price = conversion_factor * item_price
			# Add the calculated price as this barcode_uom price
			uom_prices[barcode_uom] = price
			qty = encoded_price / price if price and price > 0 else None
		return {
			"resolved_qty": qty,
			"resolved_uom": uom,
			"resolved_price": encoded_price,
			"resolved_barcode_type": barcode_type,
		}

	return None
