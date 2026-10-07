"""Per-invoice tax rate, labor taxing, and the optional card fee.

Ported from the production tests/test_backend.py.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from shopledger import services

from helpers import MIXED, PART, an_invoice


# ---- tax rate ----------------------------------------------------------------

def test_invoice_defaults_to_the_shop_rate(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART))
    assert inv["taxRate"] == 0.06625
    assert inv["taxRateLabel"] == "6.625%"
    assert inv["tax"] == 6.63


def test_one_time_rate_does_not_move_the_default(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=8))
    assert inv["tax"] == 8.0
    assert services.tax_rate(conn) == Decimal("0.06625")   # shop default untouched


def test_saving_as_default_moves_it_for_the_next_invoice(conn):
    an_invoice(conn, items=PART, tax_rate=7, save_tax_default=True)
    assert services.tax_rate(conn) == Decimal("0.07")
    nxt = services.get_invoice(conn, an_invoice(conn, items=PART, name="John Sample"))
    assert nxt["taxRate"] == 0.07


def test_history_keeps_the_rate_it_was_written_at(conn):
    no = an_invoice(conn, items=PART, tax_rate=6.625)
    services.set_default_tax_rate(conn, 9)
    assert services.get_invoice(conn, no)["tax"] == 6.63       # unchanged
    assert services.list_history(conn)[0]["invoices"][0]["tax"] == 6.63


def test_tax_can_be_switched_off_for_an_invoice(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=0))
    assert inv["tax"] == 0.0
    assert inv["total"] == 100.0
    assert inv["taxRateLabel"] == "0%"


def test_editing_an_invoice_keeps_its_rate(conn):
    no = an_invoice(conn, status="open", items=PART, tax_rate=8)
    inv = services.get_invoice(conn, no)
    services.save_invoice(conn, {"no": no, "customer_id": inv["customerId"],
                                 "status": "completed", "items": PART})
    assert services.get_invoice(conn, no)["taxRate"] == 0.08


def test_rate_accepts_a_percentage_and_rejects_nonsense():
    assert services.normalize_tax_rate("6.625") == Decimal("0.066250")
    assert services.normalize_tax_rate(6.625) == Decimal("0.066250")
    for bad in (-1, 40, "abc", "NaN", "Infinity", "-Infinity"):
        with pytest.raises(services.ValidationError):
            services.normalize_tax_rate(bad)


@pytest.mark.parametrize("pct, fraction", [
    (0.5, "0.005"), (1, "0.01"), (3, "0.03"), (6.625, "0.06625"), ("1", "0.01")])
def test_every_rate_is_a_percentage_even_at_or_below_one(conn, pct, fraction):
    # 1 used to be read as a fraction (100%) and rejected as too high.
    assert services.normalize_tax_rate(pct) == Decimal(fraction)
    assert services.normalize_cc_fee_rate(pct) == Decimal(fraction)
    assert services.set_default_tax_rate(conn, pct)["tax"] == float(fraction)
    assert services.set_default_cc_fee(conn, pct)["ccFeeRate"] == float(fraction)
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=pct, cc_fee_rate=pct))
    assert (inv["taxRate"], inv["ccFeeRate"]) == (float(fraction), float(fraction))


def test_labor_is_untaxed_by_default(conn):
    assert services.tax_labor(conn) is False
    inv = services.get_invoice(conn, an_invoice(conn, items=MIXED))
    assert {it["name"]: it["taxed"] for it in inv["items"]} == {"Labor": False, "Part": True}
    assert inv["tax"] == 6.63          # parts only


def test_taxing_labor_for_one_invoice(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=MIXED, tax_labor=True))
    assert all(it["taxed"] for it in inv["items"])
    assert inv["tax"] == 13.25         # both lines
    assert services.tax_labor(conn) is False   # shop default untouched


def test_labor_choice_can_become_the_default(conn):
    an_invoice(conn, items=MIXED, tax_labor=True, save_tax_default=True)
    assert services.tax_labor(conn) is True
    nxt = services.get_invoice(conn, an_invoice(conn, items=MIXED, name="John Sample"))
    assert nxt["tax"] == 13.25         # new invoices now tax labor too


def test_explicit_line_flag_beats_the_labor_default(conn):
    inv = services.get_invoice(conn, an_invoice(conn, tax_labor=True, items=[
        {"name": "Warranty Labor", "qty": 1, "price": 100, "type": "labor", "taxed": False},
        {"name": "Labor", "qty": 1, "price": 100, "type": "labor"},
    ]))
    assert {it["name"]: it["taxed"] for it in inv["items"]} == {
        "Warranty Labor": False, "Labor": True}


def test_set_default_tax_rate_carries_the_labor_flag(conn):
    assert services.set_default_tax_rate(conn, 7, True)["taxLabor"] is True
    assert services.bootstrap(conn)["taxLabor"] is True
    # omitting the flag leaves it where it was
    assert services.set_default_tax_rate(conn, 8)["taxLabor"] is True


# ---- credit card fee ---------------------------------------------------------

def test_no_card_fee_by_default(conn):
    assert services.cc_fee_on(conn) is False
    assert services.cc_fee_rate(conn) == Decimal("0.03")
    inv = services.get_invoice(conn, an_invoice(conn, items=PART))
    assert inv["ccFee"] == 0.0
    assert inv["total"] == 106.63


def test_card_fee_applies_to_subtotal_plus_tax(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, cc_fee_rate=3))
    # 3% of (100.00 + 6.63) = 3.1989 -> 3.20
    assert inv["ccFee"] == 3.20
    assert inv["total"] == 109.83
    assert services.cc_fee_on(conn) is False       # shop default untouched


def test_card_fee_can_become_the_default(conn):
    assert services.set_default_cc_fee(conn, 3, True)["ccFeeOn"] is True
    nxt = services.get_invoice(conn, an_invoice(conn, items=PART, name="John Sample"))
    assert nxt["ccFee"] == 3.20


def test_existing_invoices_never_gain_a_fee_retroactively(conn):
    no = an_invoice(conn, items=PART)              # written with no fee
    services.set_default_cc_fee(conn, 3, True)     # shop turns the fee on later
    assert services.get_invoice(conn, no)["ccFee"] == 0.0
    assert services.get_invoice(conn, no)["total"] == 106.63


def test_editing_an_invoice_keeps_its_fee(conn):
    no = an_invoice(conn, status="open", items=PART, cc_fee_rate=2)
    inv = services.get_invoice(conn, no)
    services.save_invoice(conn, {"no": no, "customer_id": inv["customerId"],
                                 "status": "completed", "items": PART})
    assert services.get_invoice(conn, no)["ccFeeRate"] == 0.02


def test_card_fee_is_not_itself_taxed(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=0, cc_fee_rate=3))
    assert (inv["tax"], inv["ccFee"], inv["total"]) == (0.0, 3.0, 103.0)


def test_card_fee_rate_is_validated():
    assert services.normalize_cc_fee_rate("3") == Decimal("0.030000")
    for bad in (-1, 50, "abc", "NaN", "Infinity"):
        with pytest.raises(services.ValidationError):
            services.normalize_cc_fee_rate(bad)


def test_duplicate_carries_the_card_fee(conn):
    no = an_invoice(conn, items=PART, cc_fee_rate=3)
    assert services.duplicate_invoice(conn, no)["ccFeeRate"] == 0.03


def test_duplicate_carries_the_source_rate(conn):
    no = an_invoice(conn, items=PART, tax_rate=8)
    assert services.duplicate_invoice(conn, no)["taxRate"] == 0.08
