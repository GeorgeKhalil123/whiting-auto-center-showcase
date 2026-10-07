"""End-to-end walkthrough on a throwaway database.

    python -m shopledger.demo [--out sample_invoice.pdf]

Creates a temp DB (schema + toy seed), writes a customer, vehicle and invoice
through the same `Api` the desktop window uses, forces a write to fail halfway
through to show the rollback, prints the totals, and renders a PDF receipt.
Nothing outside the temp directory and the output file is touched.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path

from . import db as db_module
from . import services
from .api import Api


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _snapshot(conn: sqlite3.Connection) -> dict:
    return {
        "customers": _count(conn, "customers"),
        "invoices": _count(conn, "invoices"),
        "next_invoice_no": db_module.get_setting(conn, "next_invoice_no"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="sample_invoice.pdf",
                        help="where to write the rendered receipt")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="shopledger-") as tmp:
        os.environ["SHOPLEDGER_HOME"] = tmp
        db_file = Path(tmp) / "shop.db"
        api = Api(db_module.init_db(db_file), backup_dir=Path(tmp) / "backups")
        conn = api._conn
        print(f"database      {db_file}  (schema v{db_module.get_setting(conn, 'schema_version')})")

        # 1. A normal write: customer -> vehicle -> invoice, each its own txn.
        cust = api.create_customer({"name": "Jane Sample", "phone": "555-0100"})
        veh = api.add_vehicle(cust["id"], {"year": 2018, "make": "Honda",
                                           "model": "Civic", "plate": "DEMO-01",
                                           "mileage": 84210})
        saved = api.save_invoice({
            "customer_id": cust["id"], "vehicle_id": veh["id"], "mileage": 84210,
            "status": "completed", "cc_fee_rate": 3,
            "comments": "Rear pads at 4mm - recheck at next oil change.",
            "items": [
                {"name": "Oil & Filter Change", "qty": 1, "price": 40, "type": "labor"},
                {"name": "Spin-on Oil Filter", "qty": 1, "price": 11, "type": "part"},
                {"name": "Brake Service (per axle)", "qty": 1, "price": 100,
                 "type": "labor"},
                {"name": "Brake Pad Set", "qty": 1, "price": 70, "type": "part"},
            ],
        })
        print(f"saved invoice #{saved['no']}  totals {saved['totals']}")
        before = _snapshot(conn)

        # 2. A write that fails halfway: the customer row and a new invoice are
        #    already inserted (and the number counter advanced) when it blows up.
        def half_done(c: sqlite3.Connection) -> None:
            ghost = services.create_customer(c, {"name": "Never Saved"})
            services.save_invoice(c, {"customer_id": ghost["id"], "items": [
                {"name": "Oil & Filter Change", "qty": 1, "price": 40, "type": "labor"}]})
            raise RuntimeError("simulated crash mid-transaction")

        result = api._write(half_done)
        after = _snapshot(conn)
        print(f"failing write -> {result}")
        print(f"  before {before}")
        print(f"  after  {after}")
        print("  rolled back: " + ("yes" if before == after else "NO"))

        # 3. A validation failure comes back as data, not an exception.
        print(f"bad input     -> {api.save_invoice({'customer_id': cust['id'], 'items': []})}")

        inv = api.get_invoice(saved["no"])
        print(f"invoice #{inv['no']}: subtotal ${inv['subtotal']:.2f}  "
              f"{'tax (' + inv['taxRateLabel'] + ')':<16} ${inv['tax']:.2f}  "
              f"card fee ${inv['ccFee']:.2f}  total ${inv['total']:.2f}")

        out = Path(args.out).resolve()
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # print_invoice reports the unwritable path below
        printed = api.print_invoice(saved["no"], dest=str(out), open_file=False)
        if printed.get("ok"):
            print(f"receipt       {printed['path']}")
        else:
            print(f"receipt       FAILED: {printed.get('error')}")
        backed = api.backup_now()
        print(f"backup        {Path(backed['path']).name}")
        api.close()
        return 0 if before == after and printed.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
