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
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=0.08))
    assert inv["tax"] == 8.0
    assert services.tax_rate(conn) == Decimal("0.06625")   # shop default untouched


def test_saving_as_default_moves_it_for_the_next_invoice(conn):
    an_invoice(conn, items=PART, tax_rate=0.07, save_tax_default=True)
    assert services.tax_rate(conn) == Decimal("0.07")
    nxt = services.get_invoice(conn, an_invoice(conn, items=PART, name="John Sample"))
    assert nxt["taxRate"] == 0.07


def test_history_keeps_the_rate_it_was_written_at(conn):
    no = an_invoice(conn, items=PART, tax_rate=0.06625)
    services.set_default_tax_rate(conn, 0.09)
    assert services.get_invoice(conn, no)["tax"] == 6.63       # unchanged
    assert services.list_history(conn)[0]["invoices"][0]["tax"] == 6.63


def test_tax_can_be_switched_off_for_an_invoice(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=0))
    assert inv["tax"] == 0.0
    assert inv["total"] == 100.0
    assert inv["taxRateLabel"] == "0%"


def test_editing_an_invoice_keeps_its_rate(conn):
    no = an_invoice(conn, status="open", items=PART, tax_rate=0.08)
    inv = services.get_invoice(conn, no)
    services.save_invoice(conn, {"no": no, "customer_id": inv["customerId"],
                                 "status": "completed", "items": PART})
    assert services.get_invoice(conn, no)["taxRate"] == 0.08


def test_rate_is_a_fraction_and_rejects_nonsense():
    # Production also accepted "6.625" and guessed it meant percent; the
    # showcase takes fractions only, so 6.625 is out of range, not 6.625%.
    assert services.normalize_tax_rate("0.06625") == Decimal("0.066250")
    assert services.normalize_tax_rate(0.06625) == Decimal("0.066250")
    for bad in (-1, 40, 6.625, "abc", "NaN", "Infinity", "-Infinity"):
        with pytest.raises(services.ValidationError):
            services.normalize_tax_rate(bad)


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
    assert services.set_default_tax_rate(conn, 0.07, True)["taxLabor"] is True
    assert services.bootstrap(conn)["taxLabor"] is True
    # omitting the flag leaves it where it was
    assert services.set_default_tax_rate(conn, 0.08)["taxLabor"] is True


# ---- credit card fee ---------------------------------------------------------

def test_no_card_fee_by_default(conn):
    assert services.cc_fee_on(conn) is False
    assert services.cc_fee_rate(conn) == Decimal("0.03")
    inv = services.get_invoice(conn, an_invoice(conn, items=PART))
    assert inv["ccFee"] == 0.0
    assert inv["total"] == 106.63


def test_card_fee_applies_to_subtotal_plus_tax(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, cc_fee_rate=0.03))
    # 3% of (100.00 + 6.63) = 3.1989 -> 3.20
    assert inv["ccFee"] == 3.20
    assert inv["total"] == 109.83
    assert services.cc_fee_on(conn) is False       # shop default untouched


def test_card_fee_can_become_the_default(conn):
    assert services.set_default_cc_fee(conn, 0.03, True)["ccFeeOn"] is True
    nxt = services.get_invoice(conn, an_invoice(conn, items=PART, name="John Sample"))
    assert nxt["ccFee"] == 3.20


def test_existing_invoices_never_gain_a_fee_retroactively(conn):
    no = an_invoice(conn, items=PART)              # written with no fee
    services.set_default_cc_fee(conn, 0.03, True)  # shop turns the fee on later
    assert services.get_invoice(conn, no)["ccFee"] == 0.0
    assert services.get_invoice(conn, no)["total"] == 106.63


def test_editing_an_invoice_keeps_its_fee(conn):
    no = an_invoice(conn, status="open", items=PART, cc_fee_rate=0.02)
    inv = services.get_invoice(conn, no)
    services.save_invoice(conn, {"no": no, "customer_id": inv["customerId"],
                                 "status": "completed", "items": PART})
    assert services.get_invoice(conn, no)["ccFeeRate"] == 0.02


def test_card_fee_is_not_itself_taxed(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=0, cc_fee_rate=0.03))
    assert (inv["tax"], inv["ccFee"], inv["total"]) == (0.0, 3.0, 103.0)


def test_card_fee_rate_is_validated():
    assert services.normalize_cc_fee_rate("0.03") == Decimal("0.030000")
    for bad in (-1, 50, 3, "abc", "NaN", "Infinity"):
        with pytest.raises(services.ValidationError):
            services.normalize_cc_fee_rate(bad)


def test_duplicate_carries_the_card_fee(conn):
    no = an_invoice(conn, items=PART, cc_fee_rate=0.03)
    assert services.duplicate_invoice(conn, no)["ccFeeRate"] == 0.03


def test_duplicate_carries_the_source_rate(conn):
    no = an_invoice(conn, items=PART, tax_rate=0.08)
    assert services.duplicate_invoice(conn, no)["taxRate"] == 0.08


# ---- showcase additions ------------------------------------------------------

@pytest.mark.parametrize("fraction", ["0.005", "0.01", "0.03", "0.06625"])
def test_small_rates_are_fractions_not_guessed_percentages(conn, fraction):
    # 0.5% and 1% are 0.005 and 0.01. Production read 1 as 100% and 0.5 as 50%.
    rate = float(fraction)
    assert services.normalize_tax_rate(rate) == Decimal(fraction)
    assert services.normalize_cc_fee_rate(rate) == Decimal(fraction)
    assert services.set_default_tax_rate(conn, rate)["tax"] == rate
    assert services.set_default_cc_fee(conn, rate)["ccFeeRate"] == rate
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=rate, cc_fee_rate=rate))
    assert (inv["taxRate"], inv["ccFeeRate"]) == (rate, rate)


@pytest.mark.parametrize("pct", [0.5, 1, 3, 6.625])
def test_percentages_are_rejected_with_a_hint(pct):
    with pytest.raises(services.ValidationError, match=r"fraction, e\.g\. 0\.03 for 3%"):
        services.normalize_cc_fee_rate(pct)
    if pct > 0.25:
        with pytest.raises(services.ValidationError, match=r"fraction, e\.g\. 0\.06625"):
            services.normalize_tax_rate(pct)


def test_rates_finer_than_six_places_are_rejected_not_rounded():
    with pytest.raises(services.ValidationError, match="more than 6 decimal places"):
        services.normalize_tax_rate("0.0662501")
    # ...but float noise from the UI's n/100 is not "more precision"
    assert 1.1 / 100 == 0.011000000000000001
    assert services.normalize_cc_fee_rate(1.1 / 100) == Decimal("0.011")


def test_get_then_save_keeps_rates_and_totals(conn):
    no = an_invoice(conn, status="open", items=PART + [
        {"name": "Labor", "qty": 2, "price": 30.5, "type": "labor"}],
        tax_rate=0.06625, cc_fee_rate=0.03)
    before = services.get_invoice(conn, no)
    services.save_invoice(conn, before)          # the UI sends back what it got
    after = services.get_invoice(conn, no)
    keys = ("taxRate", "ccFeeRate", "subtotal", "tax", "ccFee", "total")
    assert {k: after[k] for k in keys} == {k: before[k] for k in keys}
    assert (after["tax"], after["total"]) == (6.63, 172.66)   # 161 + 6.63 + 5.03


def test_bootstrap_defaults_round_trip_through_the_setters(conn):
    services.set_default_tax_rate(conn, 0.07, True)
    services.set_default_cc_fee(conn, 0.035, True)
    before = services.bootstrap(conn)
    services.set_default_tax_rate(conn, before["tax"], before["taxLabor"])
    services.set_default_cc_fee(conn, before["ccFeeRate"], before["ccFeeOn"])
    after = services.bootstrap(conn)
    keys = ("tax", "taxLabor", "ccFeeRate", "ccFeeOn")
    assert {k: after[k] for k in keys} == {k: before[k] for k in keys} == {
        "tax": 0.07, "taxLabor": True, "ccFeeRate": 0.035, "ccFeeOn": True}


@pytest.mark.parametrize("zero", ["-0", -0.0, "-0.000"])
def test_negative_zero_rate_is_plain_zero(conn, zero):
    assert str(services.normalize_tax_rate(zero)) == "0.000000"
    inv = services.get_invoice(conn, an_invoice(conn, items=PART, tax_rate=zero, cc_fee_rate=zero))
    assert inv["taxRateLabel"] == "0%" and inv["ccFeeRateLabel"] == "0%"


@pytest.mark.parametrize("flag, taxed", [(True, True), (False, False), ("true", True),
                                         ("false", False), ("False", False), (1, True),
                                         (0, False), ("1", True), ("0", False)])
def test_tax_labor_flag_is_parsed_strictly(conn, flag, taxed):
    # bool("false") is True: production taxed labor when the UI sent "false".
    inv = services.get_invoice(conn, an_invoice(conn, items=MIXED, tax_labor=flag))
    assert inv["items"][0]["taxed"] is taxed
    services.set_default_tax_rate(conn, 0.07, flag)
    assert services.tax_labor(conn) is taxed
    services.set_default_cc_fee(conn, 0.03, flag)
    assert services.cc_fee_on(conn) is taxed


@pytest.mark.parametrize("flag", ["yes", "no", 2, 0.5, [], {}, "off"])
def test_ambiguous_flags_are_rejected(conn, flag):
    with pytest.raises(services.ValidationError, match="must be true or false"):
        an_invoice(conn, items=MIXED, tax_labor=flag)
    with pytest.raises(services.ValidationError, match="must be true or false"):
        services.set_default_tax_rate(conn, 0.07, flag)
    with pytest.raises(services.ValidationError, match="must be true or false"):
        an_invoice(conn, items=[{**MIXED[0], "taxed": flag}])


def test_line_taxed_and_save_default_flags_accept_strings(conn):
    inv = services.get_invoice(conn, an_invoice(conn, items=[{**MIXED[1], "taxed": "false"}],
                                                tax_rate=0.08, save_tax_default="false"))
    assert inv["items"][0]["taxed"] is False
    assert services.tax_rate(conn) == Decimal("0.06625")    # "false" did not save it
