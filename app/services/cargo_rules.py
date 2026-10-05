"""Standalone cargo cost helpers.

The canonical anti-fraud algorithm lives in CheckoutEngine.calculate_cargo().
This module provides constants and a lightweight helper for simpler contexts.
"""

from decimal import Decimal

FREE_SHIPPING_THRESHOLD = Decimal("3000.00")
HEAVY_CARGO_MAX_RATIO = Decimal("0.50")
FLAT_SHIPPING_RATE = Decimal("49.90")
HEAVY_CARGO_SURCHARGE = Decimal("89.90")
