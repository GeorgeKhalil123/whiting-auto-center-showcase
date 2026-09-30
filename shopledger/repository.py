"""Repository layer — thin SQL over the SQLite connection.

Returns sqlite3.Row / lists of Row. No business rules here; shaping and money
math live in services.py.
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Sequence


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ---- customers ---------------------------------------------------------------

def list_customers(conn: sqlite3.Connection, search: str | None = None,
                   include_archived: bool = False) -> list[sqlite3.Row]:
    where = [] if include_archived else ["archived = 0"]
    params: list[Any] = []
    if search:
        like = f"%{search.strip().lower()}%"
        where.append("(lower(name) LIKE ? OR lower(COALESCE(phone,'')) LIKE ?)")
        params += [like, like]
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"SELECT * FROM customers{clause} ORDER BY name COLLATE NOCASE", params
    ).fetchall()


def get_customer(conn: sqlite3.Connection, cid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()


def create_customer(conn: sqlite3.Connection, name: str, phone: str, since: str, notes: str) -> str:
    cid = _new_id("c")
    conn.execute(
        "INSERT INTO customers(id, name, phone, since, notes) VALUES (?, ?, ?, ?, ?)",
        (cid, name, phone, since, notes),
    )
    return cid


def update_customer(conn: sqlite3.Connection, cid: str, fields: dict[str, Any]) -> None:
    _update(conn, "customers", cid, fields, {"name", "phone", "since", "notes"})


def delete_customer(conn: sqlite3.Connection, cid: str) -> None:
    # Hard delete; vehicles cascade via ON DELETE CASCADE. Only used when the
    # customer has no invoices at all (services enforces this).
    conn.execute("DELETE FROM customers WHERE id = ?", (cid,))


def archive_customer(conn: sqlite3.Connection, cid: str) -> None:
    # Soft delete: hide from active lists but keep the row (and vehicles) so
    # completed invoices retain their customer/vehicle details in history.
    conn.execute("UPDATE customers SET archived = 1 WHERE id = ?", (cid,))


# ---- vehicles ----------------------------------------------------------------

def list_vehicles(conn: sqlite3.Connection, customer_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM vehicles WHERE customer_id = ? ORDER BY year DESC", (customer_id,)
    ).fetchall()


def get_vehicle(conn: sqlite3.Connection, vid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM vehicles WHERE id = ?", (vid,)).fetchone()


def create_vehicle(conn: sqlite3.Connection, customer_id: str, f: dict[str, Any]) -> str:
    vid = _new_id("v")
    conn.execute(
        "INSERT INTO vehicles(id, customer_id, year, make, model, plate, vin, mileage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (vid, customer_id, f.get("year"), f.get("make"), f.get("model"),
         f.get("plate"), f.get("vin"), f.get("mileage")),
    )
    return vid


def update_vehicle(conn: sqlite3.Connection, vid: str, fields: dict[str, Any]) -> None:
    _update(conn, "vehicles", vid, fields, {"year", "make", "model", "plate", "vin", "mileage"})


# ---- catalog -----------------------------------------------------------------

def list_catalog(
    conn: sqlite3.Connection, type_: str, search: str | None = None, category: str | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM catalog_items WHERE type = ?"
    params: list[Any] = [type_]
    if category and category != "All":
        sql += " AND category = ?"
        params.append(category)
    if search:
        like = f"%{search.strip().lower()}%"
        sql += " AND (lower(name) LIKE ? OR lower(COALESCE(desc,'')) LIKE ? OR lower(COALESCE(category,'')) LIKE ?)"
        params += [like, like, like]
    sql += " ORDER BY name COLLATE NOCASE"
    return conn.execute(sql, params).fetchall()


def get_catalog_item(conn: sqlite3.Connection, cid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM catalog_items WHERE id = ?", (cid,)).fetchone()


def list_categories(conn: sqlite3.Connection, type_: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT category FROM catalog_items WHERE type = ? AND category IS NOT NULL "
        "AND category != '' ORDER BY category COLLATE NOCASE",
        (type_,),
    ).fetchall()
    return [r["category"] for r in rows]


def create_catalog_item(conn: sqlite3.Connection, type_: str, name: str, price: float,
                        category: str, desc: str) -> str:
    cid = _new_id("L" if type_ == "labor" else "P")
    conn.execute(
        "INSERT INTO catalog_items(id, type, name, price, category, desc) VALUES (?, ?, ?, ?, ?, ?)",
        (cid, type_, name, price, category, desc),
    )
    return cid


def update_catalog_item(conn: sqlite3.Connection, cid: str, fields: dict[str, Any]) -> None:
    _update(conn, "catalog_items", cid, fields, {"type", "name", "price", "category", "desc"})


def delete_catalog_item(conn: sqlite3.Connection, cid: str) -> None:
    conn.execute("DELETE FROM catalog_items WHERE id = ?", (cid,))


# ---- mechanics ---------------------------------------------------------------

def list_mechanics(conn: sqlite3.Connection, include_inactive: bool = False) -> list[sqlite3.Row]:
    clause = "" if include_inactive else " WHERE active = 1"
    return conn.execute(
        f"SELECT * FROM mechanics{clause} ORDER BY sort_order, name COLLATE NOCASE"
    ).fetchall()


def get_mechanic(conn: sqlite3.Connection, mid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM mechanics WHERE id = ?", (mid,)).fetchone()


def find_mechanic_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mechanics WHERE lower(name) = lower(?)", (name.strip(),)
    ).fetchone()


def create_mechanic(conn: sqlite3.Connection, name: str, role: str, phone: str) -> str:
    mid = _new_id("m")
    row = conn.execute("SELECT MAX(sort_order) AS m FROM mechanics").fetchone()
    conn.execute(
        "INSERT INTO mechanics(id, name, role, phone, active, sort_order) VALUES (?, ?, ?, ?, 1, ?)",
        (mid, name, role, phone, (row["m"] or 0) + 1),
    )
    return mid


def update_mechanic(conn: sqlite3.Connection, mid: str, fields: dict[str, Any]) -> None:
    _update(conn, "mechanics", mid, fields, {"name", "role", "phone", "active", "sort_order"})


def deactivate_mechanic(conn: sqlite3.Connection, mid: str) -> None:
    # Soft delete: keep the row so invoices they worked on still resolve, but
    # drop them out of the pickers.
    conn.execute("UPDATE mechanics SET active = 0 WHERE id = ?", (mid,))


def delete_mechanic(conn: sqlite3.Connection, mid: str) -> None:
    conn.execute("DELETE FROM mechanics WHERE id = ?", (mid,))


def count_invoices_by_mechanic(conn: sqlite3.Connection, mid: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM invoices WHERE mechanic_id = ?", (mid,)
    ).fetchone()
    return row["n"]


def rename_invoice_mechanic(conn: sqlite3.Connection, mid: str, name: str) -> None:
    # `serviced_by` is the printed-name snapshot; keep it in step when a
    # mechanic is renamed so old receipts reprint with the corrected spelling.
    conn.execute("UPDATE invoices SET serviced_by = ? WHERE mechanic_id = ?", (name, mid))


# ---- invoices ----------------------------------------------------------------

def get_invoice(conn: sqlite3.Connection, no: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM invoices WHERE no = ?", (no,)).fetchone()


def get_invoice_items(conn: sqlite3.Connection, no: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoice_items WHERE invoice_no = ? ORDER BY sort_order, id", (no,)
    ).fetchall()


def list_invoices_by_customer(conn: sqlite3.Connection, customer_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices WHERE customer_id = ? ORDER BY no DESC", (customer_id,)
    ).fetchall()


def list_invoices_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invoices WHERE status = ? ORDER BY no DESC", (status,)
    ).fetchall()


def upsert_invoice_header(conn: sqlite3.Connection, no: int, customer_id: str, vehicle_id: str | None,
                          date: str, mileage: int | None, status: str, comments: str,
                          mechanic_id: str | None, serviced_by: str, tax_rate: float,
                          cc_fee_rate: float = 0.0) -> None:
    conn.execute(
        "INSERT INTO invoices(no, customer_id, vehicle_id, date, mileage, status, comments, "
        "                     mechanic_id, serviced_by, tax_rate, cc_fee_rate) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(no) DO UPDATE SET "
        "  customer_id=excluded.customer_id, vehicle_id=excluded.vehicle_id, "
        "  mileage=excluded.mileage, status=excluded.status, comments=excluded.comments, "
        "  mechanic_id=excluded.mechanic_id, serviced_by=excluded.serviced_by, "
        "  tax_rate=excluded.tax_rate, cc_fee_rate=excluded.cc_fee_rate",
        (no, customer_id, vehicle_id, date, mileage, status, comments, mechanic_id,
         serviced_by, tax_rate, cc_fee_rate),
    )


def replace_invoice_items(conn: sqlite3.Connection, no: int, items: Sequence[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM invoice_items WHERE invoice_no = ?", (no,))
    for order, it in enumerate(items):
        conn.execute(
            "INSERT INTO invoice_items(invoice_no, name, qty, price, type, taxed, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (no, it["name"], it["qty"], it["price"], it["type"], it["taxed"], order),
        )


def delete_invoice(conn: sqlite3.Connection, no: int) -> None:
    # invoice_items rows cascade via ON DELETE CASCADE.
    conn.execute("DELETE FROM invoices WHERE no = ?", (no,))


def max_invoice_no(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(no) AS m FROM invoices").fetchone()
    return row["m"] or 0


# ---- helpers -----------------------------------------------------------------

def _update(conn: sqlite3.Connection, table: str, pk: str, fields: dict[str, Any],
            allowed: set[str]) -> None:
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(f"UPDATE {table} SET {cols} WHERE id = ?", [*sets.values(), pk])
