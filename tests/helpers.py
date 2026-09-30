"""Builders the tests share. All names, phones and plates are made up."""
from __future__ import annotations

from shopledger import services

PART = [{"name": "Part", "qty": 1, "price": 100, "type": "part"}]

MIXED = [{"name": "Labor", "qty": 1, "price": 100, "type": "labor"},
         {"name": "Part", "qty": 1, "price": 100, "type": "part"}]


def customer_with_vehicle(conn, name="Jane Sample"):
    """Create a customer + vehicle to invoice.

    The seed ships no demo customers (a new install starts empty), so every
    invoice test builds the little bit of data it needs.
    """
    cust = services.create_customer(conn, {"name": name, "phone": "555-0100"})
    veh = services.add_vehicle(conn, cust["id"], {
        "year": 2017, "make": "Toyota", "model": "Camry", "plate": "TEST-01",
    })
    return cust["id"], veh["id"]


def an_invoice(conn, status="completed", items=None, **extra):
    """Save one invoice for a fresh customer and return its number."""
    cid, vid = customer_with_vehicle(conn, extra.pop("name", "Jane Sample"))
    payload = {
        "customer_id": cid, "vehicle_id": vid, "status": status,
        "items": items or [{"name": "Oil & Filter Change", "qty": 1, "price": 40, "type": "labor"}],
    }
    payload.update(extra)
    return services.save_invoice(conn, payload)["no"]
