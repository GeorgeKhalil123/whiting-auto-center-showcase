"""Mechanic roster rules and forward-only schema migrations.

The roster and v2/v5 migration tests are ported from the production
tests/test_backend.py (names swapped for fictional ones); the v1 migration
test is new for the showcase.
"""
from __future__ import annotations

import sqlite3

import pytest

from shopledger import db as db_module
from shopledger import services

from helpers import an_invoice, customer_with_vehicle


# ---- mechanics ---------------------------------------------------------------

def test_seed_roster_and_active_mechanic(conn):
    roster = services.list_mechanics(conn)
    assert [m["name"] for m in roster] == ["Sample Tech"]
    assert services.active_mechanic_id(conn) == roster[0]["id"]


def test_new_invoice_uses_active_mechanic(conn):
    pat = services.create_mechanic(conn, {"name": "Pat Example", "role": "Technician"})
    services.set_active_mechanic(conn, pat["id"])
    inv = services.get_invoice(conn, an_invoice(conn, status="open"))
    assert inv["mechanicId"] == pat["id"]
    assert inv["servicedBy"] == "Pat Example"


def test_invoice_keeps_its_mechanic_when_active_switches(conn):
    pat = services.create_mechanic(conn, {"name": "Pat Example"})
    cid, vid = customer_with_vehicle(conn)
    r = services.save_invoice(conn, {
        "customer_id": cid, "vehicle_id": vid, "mechanic_id": pat["id"],
        "items": [{"name": "Oil", "qty": 1, "price": 40, "type": "labor"}],
        "status": "open",
    })
    # Re-saving without naming a mechanic must not silently reassign the work.
    services.save_invoice(conn, {
        "no": r["no"], "customer_id": cid, "vehicle_id": vid,
        "items": [{"name": "Oil", "qty": 2, "price": 40, "type": "labor"}],
        "status": "open",
    })
    assert services.get_invoice(conn, r["no"])["servicedBy"] == "Pat Example"


def test_rename_mechanic_updates_printed_name(conn):
    no = an_invoice(conn)
    tech = services.list_mechanics(conn)[0]
    services.update_mechanic(conn, tech["id"], {"name": "Sample Tech Sr."})
    assert services.get_invoice(conn, no)["servicedBy"] == "Sample Tech Sr."


def test_delete_mechanic_with_history_deactivates(conn):
    no = an_invoice(conn)
    tech = services.list_mechanics(conn)[0]
    pat = services.create_mechanic(conn, {"name": "Pat Example"})
    res = services.delete_mechanic(conn, tech["id"])
    assert res["deactivated"] is True
    assert [m["id"] for m in services.list_mechanics(conn)] == [pat["id"]]
    assert services.active_mechanic_id(conn) == pat["id"]
    # history keeps the name that was printed on the receipt
    assert services.get_invoice(conn, no)["servicedBy"] == "Sample Tech"


def test_delete_mechanic_without_history_removes_row(conn):
    pat = services.create_mechanic(conn, {"name": "Pat Example"})
    res = services.delete_mechanic(conn, pat["id"])
    assert res["deactivated"] is False
    assert len(services.list_mechanics(conn, include_inactive=True)) == 1


def test_cannot_remove_last_mechanic(conn):
    tech = services.list_mechanics(conn)[0]
    with pytest.raises(services.ValidationError):
        services.delete_mechanic(conn, tech["id"])


def test_duplicate_names_rejected(conn):
    with pytest.raises(services.ValidationError):
        services.create_mechanic(conn, {"name": "sample tech"})


def test_bootstrap_exposes_roster(conn):
    b = services.bootstrap(conn)
    assert [m["name"] for m in b["mechanics"]] == ["Sample Tech"]
    assert b["activeMechanicId"] == b["mechanics"][0]["id"]


# ---- migration ---------------------------------------------------------------

def _build_old_db(path, script: str) -> None:
    old = sqlite3.connect(str(path))
    old.executescript(script)
    old.commit()
    old.close()


def test_v2_database_migrates_serviced_by_into_roster(db_file):
    """A pre-mechanics DB gains a roster built from its `serviced_by` names."""
    _build_old_db(db_file, """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT,
                                since TEXT, notes TEXT, archived INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE vehicles (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, year INTEGER,
                               make TEXT, model TEXT, plate TEXT, vin TEXT, mileage INTEGER);
        CREATE TABLE catalog_items (id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
                                    price NUMERIC NOT NULL, category TEXT, desc TEXT);
        CREATE TABLE invoices (no INTEGER PRIMARY KEY, customer_id TEXT, vehicle_id TEXT,
                               date TEXT, mileage INTEGER, status TEXT NOT NULL, comments TEXT,
                               serviced_by TEXT);
        CREATE TABLE invoice_items (id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no INTEGER NOT NULL,
                                    name TEXT NOT NULL, qty INTEGER NOT NULL, price NUMERIC NOT NULL,
                                    type TEXT NOT NULL, taxed INTEGER NOT NULL DEFAULT 0,
                                    sort_order INTEGER);
        INSERT INTO settings VALUES ('schema_version','2');
        INSERT INTO customers VALUES ('c1','Jane Sample','555-0100','2019','',0);
        INSERT INTO invoices VALUES (900,'c1',NULL,'2026-03-01',1000,'completed','','Sample Tech');
        INSERT INTO invoices VALUES (901,'c1',NULL,'2026-03-02',1200,'completed','','Lee');
        INSERT INTO invoice_items VALUES (1,900,'Oil',1,40,'labor',0,0);
        INSERT INTO invoice_items VALUES (2,901,'Oil',1,40,'labor',0,0);
    """)

    c = db_module.init_db(db_file)
    assert db_module.get_setting(c, "schema_version") == str(db_module.SCHEMA_VERSION)
    # v4 backfills the per-invoice rate from the shop default; v5 adds the card
    # fee column and leaves old invoices at zero.
    assert services.get_invoice(c, 900)["taxRate"] == 0.06625
    assert services.get_invoice(c, 900)["ccFee"] == 0.0
    names = {m["name"]: m for m in services.list_mechanics(c)}
    assert set(names) == {"Sample Tech", "Lee"}
    assert names["Sample Tech"]["role"] == "Owner"   # kept the old sidebar label
    assert services.get_invoice(c, 901)["mechanicId"] == names["Lee"]["id"]
    assert services.active_mechanic_id(c) in {m["id"] for m in names.values()}
    # seeding must not fire on a DB that already has data
    assert len(services.list_customers(c)) == 1
    c.close()


def test_v5_database_open_invoices_track_the_live_rate_after_migrating(db_file):
    """v4/v5 froze tax/fee on every pre-existing invoice, including open ones.

    v6 un-freezes only the still-open ones so they pick up tax/fee changes
    made after upgrading, while completed invoices keep their old numbers.
    """
    old = db_module.connect(db_file)
    old.executescript(db_module.SCHEMA)
    db_module.set_setting(old, "schema_version", "5")
    db_module.set_setting(old, "tax_rate", "0.06625")
    old.execute("INSERT INTO customers VALUES ('c1','Jane Sample','555-0100','2019','',0)")
    old.execute("INSERT INTO invoices(no, customer_id, date, status, tax_rate, cc_fee_rate) "
                "VALUES (900,'c1','2026-03-01','completed',0.06625,0)")
    old.execute("INSERT INTO invoices(no, customer_id, date, status, tax_rate, cc_fee_rate) "
                "VALUES (901,'c1','2026-03-02','open',0.06625,0)")
    old.execute("INSERT INTO invoice_items(invoice_no, name, qty, price, type, taxed, sort_order) "
                "VALUES (900,'Filter',1,100,'part',1,0)")
    old.execute("INSERT INTO invoice_items(invoice_no, name, qty, price, type, taxed, sort_order) "
                "VALUES (901,'Filter',1,100,'part',1,0)")
    old.commit()
    old.close()

    c = db_module.init_db(db_file)
    assert db_module.get_setting(c, "schema_version") == str(db_module.SCHEMA_VERSION)

    # Shop raises the default tax rate and turns the card fee on post-upgrade.
    services.set_default_tax_rate(c, 8)
    services.set_default_cc_fee(c, 4, True)

    # The completed invoice is a finished sale — untouched.
    done = services.get_invoice(c, 900)
    assert done["taxRate"] == 0.06625
    assert done["ccFee"] == 0.0

    # The open invoice hasn't closed out — it tracks the new live settings.
    open_inv = services.get_invoice(c, 901)
    assert open_inv["taxRate"] == 0.08
    assert open_inv["ccFeeRate"] == 0.04
    c.close()


V1_SCHEMA = """
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT,
                            since TEXT, notes TEXT);
    CREATE TABLE vehicles (id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, year INTEGER,
                           make TEXT, model TEXT, plate TEXT, vin TEXT, mileage INTEGER);
    CREATE TABLE catalog_items (id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
                                price NUMERIC NOT NULL, category TEXT, desc TEXT);
    CREATE TABLE invoices (no INTEGER PRIMARY KEY, customer_id TEXT, vehicle_id TEXT,
                           date TEXT, mileage INTEGER, status TEXT NOT NULL, comments TEXT,
                           serviced_by TEXT);
    CREATE TABLE invoice_items (id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_no INTEGER NOT NULL,
                                name TEXT NOT NULL, qty INTEGER NOT NULL, price NUMERIC NOT NULL,
                                type TEXT NOT NULL, taxed INTEGER NOT NULL DEFAULT 0,
                                sort_order INTEGER);
    INSERT INTO settings VALUES ('schema_version','1');
    INSERT INTO settings VALUES ('tax_rate','0.06625');
    INSERT INTO settings VALUES ('next_invoice_no','902');
    INSERT INTO customers VALUES ('c1','Jane Sample','555-0100','2019','');
    INSERT INTO vehicles VALUES ('v1','c1',2012,'Ford','Focus','TEST-02','',120000);
    INSERT INTO catalog_items VALUES ('X1','part','Wiper Blade',18,'General','');
    INSERT INTO invoices VALUES (900,'c1','v1','2025-11-03',119000,'completed','','Sample Tech');
    INSERT INTO invoices VALUES (901,'c1','v1','2025-12-01',120000,'open','','Sample Tech');
    INSERT INTO invoice_items VALUES (1,900,'Wiper Blade',2,18,'part',1,0);
    INSERT INTO invoice_items VALUES (2,901,'Wiper Blade',1,18,'part',1,0);
"""


def test_v1_database_walks_every_migration_without_losing_data(db_file):
    """The oldest on-disk shape (no archived flag, no roster, no per-invoice
    rates) upgrades through v2..v6 in one boot and keeps every row."""
    _build_old_db(db_file, V1_SCHEMA)

    c = db_module.init_db(db_file)
    assert db_module.get_setting(c, "schema_version") == str(db_module.SCHEMA_VERSION)

    # v2: the soft-delete column exists and existing customers are active.
    cols = {r["name"] for r in c.execute("PRAGMA table_info(customers)")}
    assert "archived" in cols
    assert [x["name"] for x in services.list_customers(c)] == ["Jane Sample"]

    # v3: roster built from serviced_by, invoices linked to it.
    roster = services.list_mechanics(c)
    assert [(m["name"], m["role"]) for m in roster] == [("Sample Tech", "Owner")]
    assert services.get_invoice(c, 900)["mechanicId"] == roster[0]["id"]

    # v4-v6: completed invoice frozen at the old default, open one tracks live.
    services.set_default_tax_rate(c, 7)
    assert services.get_invoice(c, 900)["tax"] == 2.39    # 36 * 0.06625 = 2.385
    assert services.get_invoice(c, 901)["taxRate"] == 0.07
    assert services.get_invoice(c, 900)["ccFee"] == 0.0

    # Existing data wins over the first-run seed; numbering continues.
    assert [i["name"] for i in services.list_catalog(c, "part")] == ["Wiper Blade"]
    cid = services.get_invoice(c, 900)["customerId"]
    assert services.save_invoice(c, {"customer_id": cid, "items": [
        {"name": "Wiper Blade", "qty": 1, "price": 18, "type": "part"}]})["no"] == 902

    # v2's archive path works on a migrated row: completed history is kept.
    services.delete_invoice(c, 901)
    services.delete_invoice(c, 902)
    assert services.delete_customer(c, cid)["archived"] is True
    c.commit()
    c.close()

    # Booting again is a no-op: no duplicate roster rows, version unchanged.
    c = db_module.init_db(db_file)
    assert len(services.list_mechanics(c, include_inactive=True)) == 1
    assert db_module.get_setting(c, "schema_version") == str(db_module.SCHEMA_VERSION)
    c.close()
