"""Money math — Decimal, half-up rounding to cents.

The backend is the source of truth for money. Never do stored money math with
binary floats where rounding matters: compute with Decimal, round HALF_UP to
2 places, and hand the UI numbers it can display directly.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Mapping

CENTS = Decimal("0.01")


def dec(value: object) -> Decimal:
    """Coerce a stored/int/float/str value into a Decimal without float noise."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal(0)
    # str() first so a float like 35.1 becomes Decimal('35.1'), not the full
    # binary expansion.
    return Decimal(str(value))


def money(value: object) -> Decimal:
    """Round a value HALF_UP to 2 decimal places."""
    return dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def to_number(value: Decimal) -> float:
    """Convert a cents-quantized Decimal to a JSON-friendly float for the UI."""
    return float(money(value))


def compute_totals(items: Iterable[Mapping[str, object]], tax_rate: Decimal,
                   cc_fee_rate: Decimal | None = None) -> dict[str, float]:
    """Compute subtotal / tax / card fee / total for a set of line items.

    - line_total = qty * price
    - subtotal   = Σ line_total  (all items)
    - tax        = Σ (line_total * TAX_RATE) over taxed items, HALF_UP to cents
    - cc_fee     = (subtotal + tax) * CC_FEE_RATE — the processor charges on the
                   full amount swiped, so the fee sits on top of the taxed sum
                   and is not itself taxed
    - total      = subtotal + tax + cc_fee

    Returns floats already rounded to cents.
    """
    subtotal = Decimal(0)
    taxable_base = Decimal(0)
    for it in items:
        line = dec(it["qty"]) * dec(it["price"])
        subtotal += line
        # `it` may be a dict or a sqlite3.Row; both support __getitem__.
        if int(it["taxed"] or 0):
            taxable_base += line
    tax = money(taxable_base * tax_rate)
    subtotal = money(subtotal)
    cc_fee = money((subtotal + tax) * dec(cc_fee_rate or 0))
    total = money(subtotal + tax + cc_fee)
    return {
        "subtotal": float(subtotal),
        "tax": float(tax),
        "ccFee": float(cc_fee),
        "total": float(total),
    }
