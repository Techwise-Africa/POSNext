"""
Services module for external app integrations.

This module provides clean interfaces to optional external apps,
with graceful fallbacks when they're not installed.

Names are loaded on first access so pos_next.services.barcode_parser
(pure Python) can be imported and tested without Frappe.
"""

_EXPORTS = {
	"compute_resolved_item_data": "pos_next.services.barcode",
	"is_barcode_resolver_available": "pos_next.services.barcode",
	"resolve_barcode": "pos_next.services.barcode",
}

__all__ = ["compute_resolved_item_data", "is_barcode_resolver_available", "resolve_barcode"]


def __getattr__(name):
	if name in _EXPORTS:
		import importlib

		return getattr(importlib.import_module(_EXPORTS[name]), name)
	raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
