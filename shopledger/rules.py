"""Shop rules that every other service leans on — rates, validation, coercion.

Tax and card-fee defaults live in `settings`; each invoice snapshots the rate
it was written at (see db._migrate v4-v6). Split out of services.py so the
invoice, customer and roster modules can share them without a cycle.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from . import money
from .db import get_setting, set_setting


class ValidationError(Exception):
    """Raised for invalid input; api.py turns it into {ok: False, error}."""


# The production shop's state sales-tax rate, kept so the rounding examples
# in the tests (6.625 -> 6.63) stay the real-world edge case.
DEFAULT_TAX_RATE = Decimal("0.06625")
MAX_TAX_RATE = Decimal("0.25")  # 25% — a typo guard, not a legal limit
DEFAULT_CC_FEE_RATE = Decimal("0.03")
MAX_CC_FEE_RATE = Decimal("0.10")


def tax_rate(conn: sqlite3.Connection) -> Decimal:
    """The shop default — what a brand-new invoice starts at."""
    return money.dec(get_setting(conn, "tax_rate", str(DEFAULT_TAX_RATE)))


def tax_labor(conn: sqlite3.Connection) -> bool:
    """Whether new invoices tax labor too. Off by default — parts only."""
    return (get_setting(conn, "tax_labor", "0") or "0") == "1"


def cc_fee_rate(conn: sqlite3.Connection) -> Decimal:
    """The shop's card surcharge rate — what the fee is when it's charged."""
    return money.dec(get_setting(conn, "cc_fee_rate", str(DEFAULT_CC_FEE_RATE)))


def cc_fee_on(conn: sqlite3.Connection) -> bool:
    """Whether new invoices start with the card fee applied. Off by default."""
    return (get_setting(conn, "cc_fee_on", "0") or "0") == "1"


def invoice_cc_fee_rate(conn: sqlite3.Connection, inv: sqlite3.Row) -> Decimal:
    """The card fee an invoice was written with — 0 when none was charged.

    A completed invoice is a finished sale: a missing value there means no
    fee was taken, and a later default must not add one retroactively. An
    open invoice hasn't closed out yet, so a missing value there tracks the
    shop's live fee setting instead, same as a brand-new invoice would.
    """
    stored = inv["cc_fee_rate"] if "cc_fee_rate" in inv.keys() else None
    if stored is not None:
        return money.dec(stored)
    if inv["status"] == "open":
        return cc_fee_rate(conn) if cc_fee_on(conn) else Decimal(0)
    return Decimal(0)


def invoice_tax_rate(conn: sqlite3.Connection, inv: sqlite3.Row) -> Decimal:
    """The rate an invoice was written at, falling back to the shop default.

    Saved invoices carry their own rate so history keeps reprinting the same
    numbers after the default is changed. A missing value means the rate was
    never pinned down — either a pre-v4 row, or an open invoice that hasn't
    closed out yet — so it tracks the shop's live default.
    """
    stored = inv["tax_rate"] if "tax_rate" in inv.keys() else None
    return money.dec(stored) if stored is not None else tax_rate(conn)


def _normalize_rate(value: object, what: str, max_rate: Decimal, example: str) -> Decimal:
    """Coerce a UI rate into a fraction. Accepts 0.06625 or 6.625."""
    try:
        rate = money.dec(value if value not in (None, "") else 0)
    except Exception:
        raise ValidationError(f"{what} must be a number.")
    if rate < 0:
        raise ValidationError(f"{what} cannot be negative.")
    if rate > 1:  # sent as a percentage (6.625) rather than a fraction
        rate = rate / 100
    if rate > max_rate:
        raise ValidationError(f"{what} looks too high — enter a percentage like {example}.")
    # Keep more places than money: 6.625% is 0.06625.
    return rate.quantize(Decimal("0.000001"))


def normalize_tax_rate(value: object) -> Decimal:
    return _normalize_rate(value, "Tax rate", MAX_TAX_RATE, "6.625")


def normalize_cc_fee_rate(value: object) -> Decimal:
    return _normalize_rate(value, "Card fee", MAX_CC_FEE_RATE, "3")


def format_tax_rate(rate: Decimal) -> str:
    """0.06625 -> '6.625%'. Trailing zeros trimmed so 7% prints as '7%'."""
    pct = (money.dec(rate) * 100).quantize(Decimal("0.001")).normalize()
    text = format(pct, "f")
    return f"{text}%"


def set_default_tax_rate(conn: sqlite3.Connection, value: object,
                         labor: object = None) -> dict:
    """Change the shop-wide defaults. Existing invoices keep their own settings."""
    rate = normalize_tax_rate(value)
    set_setting(conn, "tax_rate", str(rate))
    if labor is not None:
        set_setting(conn, "tax_labor", "1" if labor else "0")
    return {"ok": True, "tax": float(rate), "taxLabor": tax_labor(conn)}


def set_default_cc_fee(conn: sqlite3.Connection, value: object,
                       on: object = None) -> dict:
    """Change the shop-wide card-fee defaults. Saved invoices keep their own."""
    rate = normalize_cc_fee_rate(value)
    set_setting(conn, "cc_fee_rate", str(rate))
    if on is not None:
        set_setting(conn, "cc_fee_on", "1" if on else "0")
    return {"ok": True, "ccFeeRate": float(rate), "ccFeeOn": cc_fee_on(conn)}



def today() -> str:
    return date.today().isoformat()


# ---- small coercers ----------------------------------------------------------

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]


def _month_label(key: str) -> str:
    """'2026-04' -> 'April 2026'; falls back to the raw key if unparseable."""
    try:
        year, month = key.split("-")
        return f"{_MONTHS[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return key or "—"


def _require_type(type_: object) -> None:
    if type_ not in ("labor", "part"):
        raise ValidationError("type must be 'labor' or 'part'.")


def _to_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def _to_price(value: object) -> float:
    try:
        d = money.dec(value if value not in (None, "") else 0)
    except Exception:
        raise ValidationError("Price must be a number.")
    if d < 0:
        raise ValidationError("Price cannot be negative.")
    return float(money.money(d))
