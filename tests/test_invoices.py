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


def test_qty_minimum_one(conn):
    no = an_invoice(conn, status="open",
                    items=[{"name": "x", "qty": 0, "price": 10, "type": "labor"}])
    assert services.get_invoice(conn, no)["items"][0]["qty"] == 1


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
