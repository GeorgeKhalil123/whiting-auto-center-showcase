"""Money math, first-run state, and the invoice lifecycle.

Ported from the production tests/test_backend.py. Tests that depended on the
shop's real seed (catalog counts, starting invoice number, specific item
prices) are rewritten against the toy seed in shopledger/seed.py.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from shopledger import db as db_module
from shopledger import money, seed, services

from helpers import an_invoice, customer_with_vehicle


# ---- totals / money ----------------------------------------------------------

def test_tax_only_on_parts():
    items = [
        {"qty": 1, "price": 100, "taxed": 0},  # labor
        {"qty": 1, "price": 100, "taxed": 1},  # part
    ]
    t = money.compute_totals(items, Decimal("0.06625"))
    assert t["subtotal"] == 200.0
    assert t["tax"] == 6.63          # 100 * 0.06625 = 6.625 -> 6.63 half-up
    assert t["total"] == 206.63


def test_half_up_rounding():
    # 0.06625 * 100 = 6.625 -> rounds half up to 6.63 (float would give 6.62)
    t = money.compute_totals([{"qty": 1, "price": 100, "taxed": 1}], Decimal("0.06625"))
    assert t["tax"] == 6.63


def test_line_total_qty():
    t = money.compute_totals([{"qty": 3, "price": 55, "taxed": 1}], Decimal("0.06625"))
    assert t["subtotal"] == 165.0
    assert t["tax"] == 10.93  # 165 * 0.06625 = 10.93125 -> 10.93


# ---- what a new install starts with ------------------------------------------

def test_seed_installs_catalog_and_roster_only(conn):
    assert len(services.list_catalog(conn, "labor")) == 2
    assert len(services.list_catalog(conn, "part")) == 2
    assert [m["name"] for m in services.list_mechanics(conn)] == ["Sample Tech"]


def test_new_install_has_no_customers_or_invoices(conn):
    """The shop's own work is the only thing that ever appears on screen."""
    assert services.list_customers(conn) == []
    assert services.list_open_invoices(conn) == []
    assert services.list_history(conn) == []

    b = services.bootstrap(conn)
    assert b["customers"] == []
    assert b["invoices"] == []
    assert len(b["catalog"]) == len(seed.CATALOG) == 4
    assert b["activeMechanicId"] == b["mechanics"][0]["id"]

    d = services.dashboard_summary(conn)
    assert d["invoices_today"] == 0
    assert d["revenue_today"] == 0
    assert d["recent"] == [] and d["open"] == []


def test_numbering_starts_at_seed_value(conn):
    # The real install continues the shop's paper book; the toy seed uses 1000.
    assert db_module.get_setting(conn, "next_invoice_no") == "1000"
    assert an_invoice(conn) == 1000


def test_seeding_does_not_rerun_on_reopen(db_file):
    c = db_module.init_db(db_file)
    services.delete_catalog_item(c, "P1")
    c.commit()
    c.close()

    c = db_module.init_db(db_file)     # reopen
    assert len(services.list_catalog(c, "part")) == 1   # not re-seeded to 2
    assert len(services.list_mechanics(c)) == 1
    c.close()


# ---- invoice lifecycle -------------------------------------------------------

def test_save_assigns_sequential_numbers(conn):
    assert an_invoice(conn, status="open") == 1000
    assert an_invoice(conn, status="completed", name="John Sample") == 1001


def test_taxed_flag_defaults_from_type(conn):
    no = an_invoice(conn, status="open", items=[
        {"name": "Labor", "qty": 1, "price": 100, "type": "labor"},
        {"name": "Part", "qty": 1, "price": 100, "type": "part"},
    ])
    inv = services.get_invoice(conn, no)
    flags = {it["name"]: it["taxed"] for it in inv["items"]}
    assert flags["Labor"] is False
    assert flags["Part"] is True
    assert inv["tax"] == 6.63


def test_price_override_does_not_touch_catalog(conn):
    an_invoice(conn, status="open", items=[
        {"name": "Brake Pad Set", "qty": 1, "price": 999, "type": "part"},
    ])
    pads = [i for i in services.list_catalog(conn, "part") if i["id"] == "P2"][0]
    assert pads["price"] == 70.0  # unchanged


def test_update_in_place_keeps_number_and_date(conn):
    no = an_invoice(conn, status="open")
    before = services.get_invoice(conn, no)
    services.save_invoice(conn, {
        "no": no, "customer_id": before["customerId"], "vehicle_id": before["vehicleId"],
        "items": [{"name": "Diag", "qty": 2, "price": 110, "type": "labor"}],
        "status": "open",
    })
    after = services.get_invoice(conn, no)
    assert after["no"] == no
    assert after["date"] == before["date"]
    assert after["items"][0]["qty"] == 2


def test_duplicate_creates_fresh_open_draft(conn):
    no = an_invoice(conn, comments="Recommend front brakes within 5k miles.")
    dup = services.duplicate_invoice(conn, no)
    src = services.get_invoice(conn, no)
    assert dup["no"] == no + 1
    assert dup["status"] == "open"
    assert dup["comments"] == ""
    assert dup["date"] == services.today()
    assert [i["name"] for i in dup["items"]] == [i["name"] for i in src["items"]]


def test_save_requires_customer_and_items(conn):
    cid, vid = customer_with_vehicle(conn)
    with pytest.raises(services.ValidationError):
        services.save_invoice(conn, {"customer_id": cid, "items": [], "status": "open"})
    with pytest.raises(services.ValidationError):
        services.save_invoice(conn, {"items": [{"name": "x", "qty": 1, "price": 1, "type": "labor"}],
                                     "status": "open"})


def test_qty_must_be_a_whole_number_of_at_least_one(conn):
    # Production clamped 0 up to 1 (and truncated 1.5 to 1); the showcase
    # rejects both instead of billing a quantity nobody typed.
    with pytest.raises(services.ValidationError, match="whole number of at least 1"):
        an_invoice(conn, status="open",
                   items=[{"name": "x", "qty": 0, "price": 10, "type": "labor"}])
    no = an_invoice(conn, status="open", items=[{"name": "x", "price": 10, "type": "labor"}])
    assert services.get_invoice(conn, no)["items"][0]["qty"] == 1     # missing -> 1


def test_history_excludes_open(conn):
    open_no = an_invoice(conn, status="open")
    done_no = an_invoice(conn, status="completed", name="John Sample")
    nos = {inv["no"] for g in services.list_history(conn) for inv in g["invoices"]}
    assert done_no in nos
    assert open_no not in nos
    assert {i["no"] for i in services.list_open_invoices(conn)} == {open_no}


def test_big_invoice_totals(conn):
    # Parts are taxed, labor is not: 70 + 70 + 2*45 + 2*40 = 310 taxable.
    no = an_invoice(conn, items=[
        {"name": "Brake Service (per axle)", "qty": 2, "price": 100, "type": "labor"},
        {"name": "Brake Pad Set (front)", "qty": 1, "price": 70, "type": "part"},
        {"name": "Brake Pad Set (rear)", "qty": 1, "price": 70, "type": "part"},
        {"name": "Rotor A (each)", "qty": 2, "price": 45, "type": "part"},
        {"name": "Rotor B (each)", "qty": 2, "price": 40, "type": "part"},
    ])
    inv = services.get_invoice(conn, no)
    assert inv["subtotal"] == 510.0
    assert inv["tax"] == 20.54   # 310 * 0.06625 = 20.5375 -> 20.54 (half-up)
    assert inv["total"] == 530.54


def test_catalog_search_and_categories(conn):
    res = services.list_catalog(conn, "part", search="brake")
    assert any("Brake" in i["name"] for i in res)
    assert "Brakes" in services.list_categories(conn, "labor")


def test_dashboard_counts_todays_completed_work(conn):
    an_invoice(conn, status="completed")
    an_invoice(conn, status="open", name="John Sample")
    d = services.dashboard_summary(conn)
    assert set(d) >= {"invoices_today", "revenue_today", "recent", "open", "today"}
    assert d["invoices_today"] == 1          # the open one doesn't count
    assert len(d["open"]) == 1


# ---- showcase additions ------------------------------------------------------

def test_readding_a_line_on_a_resumed_invoice_bumps_qty(conn):
    """Resumed/duplicated lines carry no catalog id — match on name + type."""
    no = an_invoice(conn, status="open", items=[
        {"name": "Brake Pad Set", "qty": 1, "price": 70, "type": "part"}])
    lines = [{k: it[k] for k in ("name", "qty", "price", "type", "taxed")}
             for it in services.get_invoice(conn, no)["items"]]
    services.add_line(lines, {"name": "Brake Pad Set", "price": 70, "type": "part"})
    services.add_line(lines, {"name": "Brake Pad Set", "price": 70, "type": "labor"})
    assert [(it["name"], it["type"], it["qty"]) for it in lines] == [
        ("Brake Pad Set", "part", 2), ("Brake Pad Set", "labor", 1)]


@pytest.mark.parametrize("qty", [1.5, "2.5", 0, "0", -3, "-3", "abc", "NaN", "Infinity",
                                 True, 10**30, 10_000, "1e3", "+5", "1_000", "١٢", "３", [],
                                 ",,1,,", "1,0,0", "1,00", ",100", "100,"])
def test_bad_quantities_are_rejected_not_rounded(conn, qty):
    # 1.5 h of labor at $80 used to save as qty 1 / $80; 10**30 crashed SQLite.
    cid, _ = customer_with_vehicle(conn)
    with pytest.raises(services.ValidationError, match="Quantity"):
        services.save_invoice(conn, {"customer_id": cid, "items": [
            {"name": "Labor", "qty": qty, "price": 80, "type": "labor"}]})


@pytest.mark.parametrize("qty, expected", [(2, 2), ("2", 2), (2.0, 2), ("1,000", 1000),
                                           ("9,999", 9999), ("2.0", 2), (9999, 9999)])
def test_whole_number_quantities_are_accepted(conn, qty, expected):
    no = an_invoice(conn, items=[{"name": "Part", "qty": qty, "price": 1, "type": "part"}])
    assert services.get_invoice(conn, no)["items"][0]["qty"] == expected


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_prices_are_a_validation_error(price):
    with pytest.raises(services.ValidationError, match="Price must be a number"):
        services._to_price(price)


@pytest.mark.parametrize("call", [services.get_invoice, services.duplicate_invoice,
                                  services.delete_invoice])
@pytest.mark.parametrize("no", ["abc", "", None, "1.5", True, 10**30])
def test_non_numeric_invoice_numbers_are_a_validation_error(conn, call, no):
    with pytest.raises(services.ValidationError, match="Invoice number"):
        call(conn, no)


def test_search_treats_percent_and_underscore_literally(conn):
    services.create_customer(conn, {"name": "Jane Sample"})
    services.create_customer(conn, {"name": "100% Auto_Parts"})
    assert [c["name"] for c in services.list_customers(conn, "%")] == ["100% Auto_Parts"]
    assert [c["name"] for c in services.list_customers(conn, "_")] == ["100% Auto_Parts"]
    assert len(services.list_customers(conn, "sample")) == 1
    assert services.list_catalog(conn, "part", search="%") == []
    assert services.list_catalog(conn, "part", search="_") == []


def test_money_totals_are_summed_as_decimal(conn):
    # 0.1 + 0.2 + 0.7 as floats is 0.9999999999999999; as Decimal it is 1.00.
    for price, name in ((0.1, "A Sample"), (0.2, "B Sample"), (0.7, "C Sample")):
        an_invoice(conn, name=name, tax_rate=0,
                   items=[{"name": "Part", "qty": 1, "price": price, "type": "part"}])
    month = services.list_history(conn)[0]
    assert month["total"] == 1.0 and isinstance(month["total"], float)
    revenue = services.dashboard_summary(conn)["revenue_today"]
    assert revenue == 1.0 and isinstance(revenue, float)


@pytest.mark.parametrize("price", [10**30, "1e16", 1_000_000])
def test_absurd_prices_are_rejected(conn, price):
    # 10**30 overflowed the cents quantize; 1e16 lost precision as a float.
    with pytest.raises(services.ValidationError, match="Price looks too high"):
        services.create_catalog_item(conn, {"name": "X", "type": "part", "price": price})
    assert services._to_price("999999.99") == 999999.99


@pytest.mark.parametrize("vid", ["abc", -1, 1.5, True, "1e3", float("inf"), "NaN", 10**30, [], {}])
def test_save_rejects_a_vehicle_that_does_not_exist(conn, vid):
    cid, _ = customer_with_vehicle(conn)
    with pytest.raises(services.ValidationError, match="Vehicle not found"):
        services.save_invoice(conn, {"customer_id": cid, "vehicle_id": vid, "items": [
            {"name": "Part", "qty": 1, "price": 1, "type": "part"}]})


def test_save_rejects_another_customers_vehicle(conn):
    _, other_vid = customer_with_vehicle(conn, "John Sample")
    with pytest.raises(services.ValidationError, match="Vehicle not found"):
        an_invoice(conn, vehicle_id=other_vid)


@pytest.mark.parametrize("miles", [10**30, -5, 1.5, "abc", float("nan"), True])
def test_bad_mileage_is_rejected(conn, miles):
    with pytest.raises(services.ValidationError, match="Mileage"):
        an_invoice(conn, mileage=miles)
    assert services.get_invoice(conn, an_invoice(conn, mileage="84,210"))["mileage"] == 84210


@pytest.mark.parametrize("bad_id", [[], {}, object(), 10**30, 5, None, True, ""])
def test_malformed_ids_are_not_found(conn, bad_id):
    calls = [
        (services.get_customer, "Customer"), (services.delete_customer, "Customer"),
        (lambda c, i: services.update_customer(c, i, {}), "Customer"),
        (lambda c, i: services.add_vehicle(c, i, {"make": "A", "model": "B"}), "Customer"),
        (lambda c, i: services.update_vehicle(c, i, {}), "Vehicle"),
        (lambda c, i: services.update_mechanic(c, i, {}), "Mechanic"),
        (services.delete_mechanic, "Mechanic"), (services.set_active_mechanic, "Mechanic"),
        (lambda c, i: services.update_catalog_item(c, i, {}), "Catalog item"),
        (services.delete_catalog_item, "Catalog item"),
    ]
    for call, what in calls:
        with pytest.raises(services.ValidationError, match=f"{what} not found"):
            call(conn, bad_id)
    cid, _ = customer_with_vehicle(conn)
    if bad_id not in (None, ""):   # those mean "use the active mechanic"
        with pytest.raises(services.ValidationError, match="Mechanic not found"):
            an_invoice(conn, mechanic_id=bad_id)


def test_numbers_are_accepted_as_text_but_lists_are_not(conn):
    cust = services.create_customer(conn, {"name": 42, "phone": 5550100})
    assert (cust["name"], cust["phone"]) == ("42", "5550100")
    assert [c["name"] for c in services.list_customers(conn, 42)] == ["42"]
    assert services.list_history(conn, 1000) == []
    for field in ("name", "phone", "notes"):
        with pytest.raises(services.ValidationError, match="must be text"):
            services.create_customer(conn, {"name": "Ok", field: ["x"]})
    with pytest.raises(services.ValidationError, match="must be text"):
        an_invoice(conn, comments={"a": 1})
    with pytest.raises(services.ValidationError, match="must be text"):
        services.add_vehicle(conn, cust["id"], {"make": True, "model": "B"})
    with pytest.raises(services.ValidationError, match="must be text"):
        services.list_customers(conn, ["x"])


@pytest.mark.parametrize("items, msg", [
    ("abc", "must be a list"), ({"name": "A"}, "must be a list"), (5, "must be a list"),
    ([None], "needs a name"), (["x"], "needs a name"), ([{"name": None}], "needs a name")])
def test_malformed_line_items_are_rejected(conn, items, msg):
    with pytest.raises(services.ValidationError, match=msg):
        an_invoice(conn, items=items)


@pytest.mark.parametrize("year", [10**30, "abc", 1899, -1, 1.5, True])
def test_bad_vehicle_years_are_rejected_not_replaced(conn, year):
    # These used to be swapped for the current year without a word.
    cid, vid = customer_with_vehicle(conn)
    with pytest.raises(services.ValidationError, match="Year must be between 1900"):
        services.add_vehicle(conn, cid, {"make": "A", "model": "B", "year": year})
    with pytest.raises(services.ValidationError, match="Year must be between 1900"):
        services.update_vehicle(conn, vid, {"year": year})


def test_vehicle_year_range_and_blank_default(conn):
    from datetime import date
    cid, _ = customer_with_vehicle(conn)
    nxt = date.today().year + 1
    assert services.add_vehicle(conn, cid, {"make": "A", "model": "B", "year": nxt})["year"] == nxt
    assert services.add_vehicle(conn, cid, {"make": "A", "model": "B", "year": "1900"})["year"] == 1900
    blank = services.add_vehicle(conn, cid, {"make": "A", "model": "B", "year": ""})
    assert blank["year"] == date.today().year
    with pytest.raises(services.ValidationError):
        services.add_vehicle(conn, cid, {"make": "A", "model": "B", "year": nxt + 1})
