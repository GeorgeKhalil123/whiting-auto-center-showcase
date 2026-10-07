"""Shop rules that every other service leans on — rates, validation, coercion.

Tax and card-fee defaults live in `settings`; each invoice snapshots the rate
it was written at (see db._migrate v4-v6). Split out of services.py so the
invoice, customer and roster modules can share them without a cycle.
"""
from __future__ import annotations

import re
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
RATE_PLACES = Decimal("0.000001")
# More typo guards. MAX_SQL_INT is SQLite's INTEGER range.
MAX_PRICE = Decimal("999999.99")
MAX_QTY = 9999
MAX_MILEAGE = 9_999_999
MAX_SQL_INT = 2**63


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
    """Validate a rate sent as a fraction: 0.06625 is 6.625%, 0.01 is 1%.

    Production also accepted percentages and guessed (anything over 1 was
    divided by 100), which read 1 as 100%. The showcase never guesses: a
    value above the cap is rejected, not reinterpreted.
    """
    try:
        rate = money.dec(value if value not in (None, "") else 0)
    except Exception:
        raise ValidationError(f"{what} must be a number.") from None
    if not rate.is_finite():
        raise ValidationError(f"{what} must be a number.")
    if rate < 0:
        raise ValidationError(f"{what} cannot be negative.")
    if rate > max_rate:
        raise ValidationError(f"{what} must be a fraction, e.g. {example}.")
    # Six places holds any rate to a thousandth of a percent. Anything finer
    # is rejected rather than rounded away; float noise from the UI's n/100
    # (0.011000000000000001) is within the tolerance and snaps back.
    snapped = rate.quantize(RATE_PLACES)
    if abs(rate - snapped) > Decimal("1e-12"):
        raise ValidationError(f"{what} has more than 6 decimal places.")
    return snapped


def normalize_tax_rate(value: object) -> Decimal:
    return _normalize_rate(value, "Tax rate", MAX_TAX_RATE, "0.06625 for 6.625%")


def normalize_cc_fee_rate(value: object) -> Decimal:
    return _normalize_rate(value, "Card fee", MAX_CC_FEE_RATE, "0.03 for 3%")


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
        n = int(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None
    return n if -MAX_SQL_INT <= n < MAX_SQL_INT else None   # SQLite INTEGER range


def _to_price(value: object) -> float:
    try:
        d = money.dec(value if value not in (None, "") else 0)
    except Exception:
        raise ValidationError("Price must be a number.") from None
    if not d.is_finite():
        raise ValidationError("Price must be a number.")
    if d < 0:
        raise ValidationError("Price cannot be negative.")
    if d > MAX_PRICE:
        raise ValidationError(f"Price looks too high — enter at most {MAX_PRICE:,}.")
    return float(money.money(d))


def _whole_number(value: object) -> int | None:
    """'1,000' / 1000 / 1000.0 -> 1000. None for anything else: no 1.5, no
    1e3, no signs, no non-ASCII digits, no bools."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[0-9]+(\.0*)?", text):
        return None
    return int(text.split(".")[0])


def _to_qty(value: object) -> int:
    """A line's quantity: a whole number from 1 up. Missing means 1.

    Never rounds or clamps — billing 1.5 hours as 1 would undercharge silently.
    """
    if value is None or value == "":
        return 1
    qty = _whole_number(value)
    if qty is None or qty < 1:
        raise ValidationError("Quantity must be a whole number of at least 1.")
    if qty > MAX_QTY:
        raise ValidationError(f"Quantity looks too high — enter at most {MAX_QTY}.")
    return qty


def _to_mileage(value: object) -> int | None:
    """Odometer reading: blank, or a whole number of miles."""
    if value is None or value == "":
        return None
    miles = _whole_number(value)
    if miles is None or miles > MAX_MILEAGE:
        raise ValidationError("Mileage must be a whole number of miles.")
    return miles


def _to_invoice_no(value: object) -> int:
    """An invoice number from the UI; anything non-numeric can't be one."""
    no = _whole_number(value)
    if no is None or not 0 < no < MAX_SQL_INT:
        raise ValidationError("Invoice number must be a whole number.")
    return no


def _to_id(value: object, what: str) -> str:
    """A row id (customer, vehicle, mechanic, catalog item) — always a string."""
    if isinstance(value, str) and 0 < len(value) <= 64:
        return value
    raise ValidationError(f"{what} not found.")


def _given(data: dict, *keys: str) -> object:
    """The first of `keys` actually filled in. Only None and "" count as
    blank, so a malformed value like [] is validated rather than ignored."""
    return next((data[k] for k in keys if data.get(k) is not None and data[k] != ""), None)


def _text(value: object, what: str) -> str:
    """A text field, stripped. Numbers are fine (the UI may send a phone as
    one); lists, objects and booleans are not."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError(f"{what} must be text.")
    return str(value).strip()
