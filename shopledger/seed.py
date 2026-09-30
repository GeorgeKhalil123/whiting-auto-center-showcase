"""First-run seed data — a TOY price list and a one-person roster.

The production app seeds the shop's real labor/parts price list and mechanic
roster here. That data is the client's and stays private; this module keeps
the same interface (`seed(conn)`, same tables, same settings) with a handful
of fictional items so the rest of the backend behaves exactly as it does in
the shop.

A new install starts with **no customers, vehicles, or invoices**: the shop
enters its own as it works. Only the two reference tables are pre-filled, so
the dashboard, open invoices, and history all start empty.
"""
from __future__ import annotations

import sqlite3

from .db import set_setting

# (id, type, name, price, category, desc)
CATALOG: list[tuple[str, str, str, float, str, str]] = [
    ("L1", "labor", "Oil & Filter Change", 40, "Maintenance", "Drain, refill, new filter"),
    ("L2", "labor", "Brake Service (per axle)", 100, "Brakes",
     "Replace pads, inspect hardware"),
    ("P1", "part", "Spin-on Oil Filter", 11, "Fluids", "Standard-size filter"),
    ("P2", "part", "Brake Pad Set", 70, "Brakes", "Pad set, one axle"),
]

DEFAULT_MECHANIC_ID = "m-sample"
DEFAULT_MECHANIC_NAME = "Sample Tech"

# Where numbering starts on a brand-new database. The real install continues
# the shop's existing paper book; the showcase just starts at a round number.
NEXT_INVOICE_NO = 1000


def seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO catalog_items(id, type, name, price, category, desc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        CATALOG,
    )
    conn.execute(
        "INSERT INTO mechanics(id, name, role, phone, active, sort_order) "
        "VALUES (?, ?, ?, ?, 1, 0)",
        (DEFAULT_MECHANIC_ID, DEFAULT_MECHANIC_NAME, "Owner", ""),
    )
    set_setting(conn, "next_invoice_no", str(NEXT_INVOICE_NO))
    set_setting(conn, "active_mechanic_id", DEFAULT_MECHANIC_ID)
