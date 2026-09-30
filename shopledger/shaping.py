"""DTO shaping — sqlite3.Row in, JSON-ready dicts out, money rounded to cents."""
from __future__ import annotations

import sqlite3

from . import money, repository as repo
from .rules import invoice_cc_fee_rate, invoice_tax_rate


def _customer_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "phone": row["phone"] if row["phone"] else "—",
        "since": row["since"],
        "notes": row["notes"] or "",
        "archived": bool(row["archived"]) if "archived" in row.keys() else False,
    }


def _vehicle_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "customerId": row["customer_id"],
        "year": row["year"],
        "make": row["make"],
        "model": row["model"],
        "plate": row["plate"] if row["plate"] else "—",
        "vin": row["vin"] if row["vin"] else "—",
        "mileage": row["mileage"],
        "title": vehicle_title(row),
    }


def vehicle_title(row: sqlite3.Row | None) -> str:
    if not row:
        return "—"
    return f"{row['year']} {row['make']} {row['model']}".strip()


def _mechanic_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"] or "",
        "phone": row["phone"] or "",
        "active": bool(row["active"]),
    }


def _item_dict(row: sqlite3.Row) -> dict:
    line = money.to_number(money.dec(row["qty"]) * money.dec(row["price"]))
    return {
        "name": row["name"],
        "qty": row["qty"],
        "price": float(money.money(row["price"])),
        "type": row["type"],
        "taxed": bool(row["taxed"]),
        "lineTotal": line,
    }


def _invoice_summary(conn: sqlite3.Connection, inv: sqlite3.Row) -> dict:
    items = repo.get_invoice_items(conn, inv["no"])
    rate = invoice_tax_rate(conn, inv)
    fee = invoice_cc_fee_rate(conn, inv)
    totals = money.compute_totals(items, rate, fee)
    cust = repo.get_customer(conn, inv["customer_id"])
    veh = repo.get_vehicle(conn, inv["vehicle_id"]) if inv["vehicle_id"] else None
    return {
        "no": inv["no"],
        "customerId": inv["customer_id"],
        "vehicleId": inv["vehicle_id"],
        "customer": cust["name"] if cust else "—",
        "vehicle": vehicle_title(veh),
        "plate": (veh["plate"] if veh and veh["plate"] else "—"),
        "date": inv["date"],
        "mileage": inv["mileage"],
        "status": inv["status"],
        "comments": inv["comments"] or "",
        "mechanicId": inv["mechanic_id"],
        "servicedBy": _serviced_by(conn, inv),
        "taxRate": float(rate),
        "ccFeeRate": float(fee),
        "itemCount": len(items),
        **totals,
    }


def _serviced_by(conn: sqlite3.Connection, inv: sqlite3.Row) -> str:
    """Name to print for an invoice: the snapshot, else the linked mechanic."""
    if inv["serviced_by"]:
        return inv["serviced_by"]
    mech = repo.get_mechanic(conn, inv["mechanic_id"]) if inv["mechanic_id"] else None
    return mech["name"] if mech else "—"



def _catalog_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "price": float(money.money(row["price"])),
        "category": row["category"] or "General",
        "desc": row["desc"] or "",
    }
