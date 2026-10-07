"""Service layer — business rules, invoice numbering, and DTO shaping.

Everything the API returns is JSON-serializable and money is pre-rounded to
cents. Writes happen inside a single transaction managed by the caller (api.py).

Rates/validation (rules.py), row shaping (shaping.py), the mechanic roster
(roster.py) and the catalog (catalog.py) are re-exported here so callers only
ever import `services`.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from . import money, repository as repo
from .db import get_setting, set_setting
from .roster import (active_mechanic_id, create_mechanic, delete_mechanic,  # noqa: F401
                     list_mechanics, set_active_mechanic, update_mechanic,
                     _resolve_mechanic)
from .rules import (DEFAULT_CC_FEE_RATE, DEFAULT_TAX_RATE, MAX_CC_FEE_RATE,  # noqa: F401
                    MAX_TAX_RATE, ValidationError, cc_fee_on, cc_fee_rate,
                    format_tax_rate, invoice_cc_fee_rate, invoice_tax_rate,
                    normalize_cc_fee_rate, normalize_tax_rate, set_default_cc_fee,
                    set_default_tax_rate, tax_labor, tax_rate, today,
                    _given, _month_label, _require_type, _text, _to_id, _to_int,
                    _to_invoice_no, _to_mileage, _to_price, _to_qty)
from .catalog import (create_catalog_item, delete_catalog_item,  # noqa: F401
                      list_catalog, list_categories, update_catalog_item)
from .shaping import (_catalog_dict, _customer_dict, _invoice_summary,  # noqa: F401
                      _item_dict, _serviced_by, _vehicle_dict, vehicle_title)


def bootstrap(conn: sqlite3.Connection) -> dict:
    """One round-trip load of everything in the frontend's `this.state` shape.

    The UI hydrates `catalog`, `customers` (with nested `vehicles`), and
    `invoices` (with nested `items`) from this, then renders exactly as before.
    """
    catalog = ([_catalog_dict(r) for r in repo.list_catalog(conn, "labor")]
               + [_catalog_dict(r) for r in repo.list_catalog(conn, "part")])

    # Include archived customers so completed invoices in history can still
    # resolve their customer/vehicle names; the frontend hides archived ones
    # from the active customer list and pickers.
    customers = []
    for c in repo.list_customers(conn, include_archived=True):
        cust = _customer_dict(c)
        cust["vehicles"] = [_vehicle_dict(v) for v in repo.list_vehicles(conn, c["id"])]
        customers.append(cust)

    invoices = []
    for inv in repo.list_invoices_by_status(conn, "open") + repo.list_invoices_by_status(conn, "completed"):
        items = repo.get_invoice_items(conn, inv["no"])
        invoices.append({
            "no": inv["no"],
            "customerId": inv["customer_id"],
            "vehicleId": inv["vehicle_id"],
            "date": inv["date"],
            "mileage": inv["mileage"],
            "status": inv["status"],
            "comments": inv["comments"] or "",
            "mechanicId": inv["mechanic_id"],
            "servicedBy": inv["serviced_by"] or "",
            "taxRate": float(invoice_tax_rate(conn, inv)),
            "ccFeeRate": float(invoice_cc_fee_rate(conn, inv)),
            "items": [{"name": r["name"], "qty": r["qty"],
                       "price": float(money.money(r["price"])),
                       "type": r["type"], "taxed": bool(r["taxed"])}
                      for r in items],
        })

    next_no = int(get_setting(conn, "next_invoice_no", "1") or "1")
    return {
        "tax": float(tax_rate(conn)),
        "taxLabor": tax_labor(conn),
        "ccFeeRate": float(cc_fee_rate(conn)),
        "ccFeeOn": cc_fee_on(conn),
        "today": today(),
        "nextNo": next_no,
        "catalog": catalog,
        "customers": customers,
        "invoices": invoices,
        "mechanics": list_mechanics(conn),
        "activeMechanicId": active_mechanic_id(conn),
    }

# ---- customers & vehicles ----------------------------------------------------

def list_customers(conn: sqlite3.Connection, search: str | None = None) -> list[dict]:
    return [_customer_dict(r) for r in repo.list_customers(conn, _text(search, "Search"))]


def get_customer(conn: sqlite3.Connection, cid: str) -> dict:
    row = repo.get_customer(conn, cid := _to_id(cid, "Customer"))
    if not row:
        raise ValidationError("Customer not found.")
    vehicles = [_vehicle_dict(v) for v in repo.list_vehicles(conn, cid)]
    invoices = [_invoice_summary(conn, i) for i in repo.list_invoices_by_customer(conn, cid)]
    return {
        "customer": _customer_dict(row),
        "vehicles": vehicles,
        "invoices": invoices,
        "invoiceCount": len(invoices),
    }


def create_customer(conn: sqlite3.Connection, data: dict) -> dict:
    name = _text(data.get("name"), "Customer name")
    if not name:
        raise ValidationError("Customer name is required.")
    phone = _text(data.get("phone"), "Phone") or "—"
    notes = _text(data.get("notes"), "Notes")
    since = _text(data.get("since"), "Customer since") or str(date.today().year)
    cid = repo.create_customer(conn, name, phone, since, notes)
    return _customer_dict(repo.get_customer(conn, cid))


def update_customer(conn: sqlite3.Connection, cid: str, data: dict) -> dict:
    if not repo.get_customer(conn, cid := _to_id(cid, "Customer")):
        raise ValidationError("Customer not found.")
    fields: dict = {}
    if "name" in data:
        if not (name := _text(data["name"], "Customer name")):
            raise ValidationError("Customer name is required.")
        fields["name"] = name
    if "phone" in data:
        fields["phone"] = _text(data["phone"], "Phone") or "—"
    if "notes" in data:
        fields["notes"] = _text(data["notes"], "Notes")
    if "since" in data:
        fields["since"] = _text(data["since"], "Customer since")
    repo.update_customer(conn, cid, fields)
    return _customer_dict(repo.get_customer(conn, cid))


def delete_customer(conn: sqlite3.Connection, cid: str) -> dict:
    """Remove a customer from the active roster.

    - Blocked if they have any OPEN invoices (finish or delete those first).
    - If they have only COMPLETED invoices, the customer is archived (soft
      delete): hidden from active lists but kept in the DB so those invoices —
      and their customer/vehicle details — stay intact in history.
    - If they have no invoices at all, the customer (and vehicles) are deleted
      outright.
    """
    if not repo.get_customer(conn, cid := _to_id(cid, "Customer")):
        raise ValidationError("Customer not found.")
    invoices = repo.list_invoices_by_customer(conn, cid)
    open_count = sum(1 for i in invoices if i["status"] == "open")
    if open_count:
        raise ValidationError(
            f"This customer has {open_count} open invoice"
            f"{'s' if open_count != 1 else ''}. Delete or complete "
            "them first, then you can remove the customer."
        )
    if invoices:  # only completed invoices remain — keep them as history
        repo.archive_customer(conn, cid)
        return {"ok": True, "id": cid, "archived": True}
    repo.delete_customer(conn, cid)
    return {"ok": True, "id": cid, "archived": False}


def add_vehicle(conn: sqlite3.Connection, customer_id: str, data: dict) -> dict:
    if not repo.get_customer(conn, customer_id := _to_id(customer_id, "Customer")):
        raise ValidationError("Customer not found.")
    make = _text(data.get("make"), "Make")
    model = _text(data.get("model"), "Model")
    if not make or not model:
        raise ValidationError("Vehicle make and model are required.")
    f = {
        "year": _to_int(data.get("year")) or date.today().year,
        "make": make,
        "model": model,
        "plate": _text(data.get("plate"), "Plate") or "—",
        "vin": _text(data.get("vin"), "VIN") or "—",
        "mileage": _to_mileage(data.get("mileage")),
    }
    vid = repo.create_vehicle(conn, customer_id, f)
    return _vehicle_dict(repo.get_vehicle(conn, vid))


def update_vehicle(conn: sqlite3.Connection, vid: str, data: dict) -> dict:
    if not repo.get_vehicle(conn, vid := _to_id(vid, "Vehicle")):
        raise ValidationError("Vehicle not found.")
    fields: dict = {}
    for key in ("make", "model", "plate", "vin"):
        if key in data:
            fields[key] = _text(data[key], key) or ("—" if key in ("plate", "vin") else "")
    if "year" in data:
        fields["year"] = _to_int(data.get("year"))
    if "mileage" in data:
        fields["mileage"] = _to_mileage(data.get("mileage"))
    repo.update_vehicle(conn, vid, fields)
    return _vehicle_dict(repo.get_vehicle(conn, vid))


# ---- invoices ----------------------------------------------------------------

def _next_invoice_no(conn: sqlite3.Connection) -> int:
    """Monotonic invoice number, safe against duplicates.

    Take the greater of the stored counter and MAX(no)+1, then advance the
    counter past it. Never reuses a number.
    """
    counter = int(get_setting(conn, "next_invoice_no", "1") or "1")
    candidate = max(counter, repo.max_invoice_no(conn) + 1)
    set_setting(conn, "next_invoice_no", str(candidate + 1))
    return candidate


def _normalize_items(raw: list[dict], labor_taxed: bool = False) -> list[dict]:
    if not isinstance(raw, (list, tuple)):
        raise ValidationError("Line items must be a list.")
    items: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            raise ValidationError("Every line item needs a name, qty, price and type.")
        name = _text(it.get("name"), "Item name")
        if not name:
            raise ValidationError("Every line item needs a name.")
        type_ = it.get("type")
        if type_ not in ("labor", "part"):
            raise ValidationError("Line item type must be 'labor' or 'part'.")
        qty = _to_qty(it.get("qty"))
        price = _to_price(it.get("price"))
        # taxed defaults to (type == 'part'), or to everything when the invoice
        # taxes labor too. A stored/explicit per-line flag always wins.
        taxed = it.get("taxed")
        default_taxed = type_ == "part" or labor_taxed
        taxed_flag = default_taxed if taxed is None else (1 if taxed else 0)
        items.append({
            "name": name, "qty": qty, "price": price, "type": type_,
            "taxed": 1 if taxed_flag else 0,
        })
    return items


def save_invoice(conn: sqlite3.Connection, data: dict) -> dict:
    """Create (no `no`) or update an invoice in place. Returns {no, totals}."""
    customer_id = data.get("customer_id") or data.get("customerId")
    if not customer_id:
        raise ValidationError("Select a customer before saving.")
    if not repo.get_customer(conn, customer_id := _to_id(customer_id, "Customer")):
        raise ValidationError("Customer not found.")

    raw_labor = data.get("tax_labor", data.get("taxLabor"))
    labor_taxed = tax_labor(conn) if raw_labor is None else bool(raw_labor)
    items = _normalize_items(data.get("items") or [], labor_taxed)
    if not items:
        raise ValidationError("Add at least one item before saving.")

    status = data.get("status", "open")
    if status not in ("open", "completed"):
        raise ValidationError("Status must be 'open' or 'completed'.")

    vehicle_id = _given(data, "vehicle_id", "vehicleId")
    if vehicle_id is not None:   # must exist and be this customer's car
        veh = repo.get_vehicle(conn, vehicle_id := _to_id(vehicle_id, "Vehicle"))
        if not veh or veh["customer_id"] != customer_id:
            raise ValidationError("Vehicle not found.")
    mileage = _to_mileage(data.get("mileage"))
    comments = _text(data.get("comments"), "Comments")

    no = data.get("no")
    existing = None
    if no is None:
        no = _next_invoice_no(conn)
        inv_date = today()
    else:
        no = _to_invoice_no(no)
        existing = repo.get_invoice(conn, no)
        inv_date = existing["date"] if existing else today()

    mechanic_id, serviced_by = _resolve_mechanic(conn, data, existing)
    rate = _resolve_tax_rate(conn, data, existing)
    fee = _resolve_cc_fee_rate(conn, data, existing)

    repo.upsert_invoice_header(conn, no, customer_id, vehicle_id, inv_date, mileage,
                               status, comments, mechanic_id, serviced_by,
                               float(rate), float(fee))
    repo.replace_invoice_items(conn, no, items)

    if data.get("save_tax_default") or data.get("saveTaxDefault"):
        set_setting(conn, "tax_rate", str(rate))
        set_setting(conn, "tax_labor", "1" if labor_taxed else "0")

    totals = money.compute_totals(items, rate, fee)
    return {"no": no, "totals": totals, "status": status, "date": inv_date,
            "mechanicId": mechanic_id, "servicedBy": serviced_by,
            "taxRate": float(rate), "ccFeeRate": float(fee)}


def add_line(items: list[dict], line: dict) -> list[dict]:
    """Add a catalog pick to a draft's lines, bumping qty if it's already there.

    Ported from the UI's addToDraft. Lines are matched by name + type, not by
    catalog id: saved items never persist which catalog entry they came from,
    so a resumed or duplicated invoice loads every line with no id — matching
    on id made re-adding a part to a reopened invoice push a duplicate line.
    """
    key = ((line.get("name") or "").strip().lower(), line.get("type"))
    for it in items:
        if ((it.get("name") or "").strip().lower(), it.get("type")) == key:
            it["qty"] = (_to_int(it.get("qty")) or 0) + (_to_int(line.get("qty")) or 1)
            return items
    items.append({**line, "qty": _to_int(line.get("qty")) or 1})
    return items


def _resolve_cc_fee_rate(conn: sqlite3.Connection, data: dict,
                         existing: sqlite3.Row | None) -> Decimal:
    """Card fee for a save: the explicit one, else the invoice's own.

    A brand-new invoice only gets a fee if the shop default says to charge one;
    an existing one keeps whatever it already had.
    """
    raw = data.get("cc_fee_rate", data.get("ccFeeRate"))
    if raw is not None and raw != "":
        return normalize_cc_fee_rate(raw)
    if existing is not None:
        return invoice_cc_fee_rate(conn, existing)
    return cc_fee_rate(conn) if cc_fee_on(conn) else Decimal(0)


def _resolve_tax_rate(conn: sqlite3.Connection, data: dict,
                      existing: sqlite3.Row | None) -> Decimal:
    """Rate for a save: the explicit one, else the invoice's own, else default."""
    raw = data.get("tax_rate", data.get("taxRate"))
    if raw is not None and raw != "":
        return normalize_tax_rate(raw)
    if existing is not None:
        return invoice_tax_rate(conn, existing)
    return tax_rate(conn)


def get_invoice(conn: sqlite3.Connection, no: int) -> dict:
    inv = repo.get_invoice(conn, _to_invoice_no(no))
    if not inv:
        raise ValidationError("Invoice not found.")
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
        "phone": (cust["phone"] if cust and cust["phone"] else "—"),
        "vehicle": vehicle_title(veh),
        "plate": (veh["plate"] if veh and veh["plate"] else "—"),
        "date": inv["date"],
        "mileage": inv["mileage"],
        "status": inv["status"],
        "comments": inv["comments"] or "",
        "mechanicId": inv["mechanic_id"],
        "servicedBy": _serviced_by(conn, inv),
        "taxRate": float(rate),
        "taxRateLabel": format_tax_rate(rate),
        "ccFeeRate": float(fee),
        "ccFeeRateLabel": format_tax_rate(fee),
        "items": [_item_dict(r) for r in items],
        **totals,
    }


def list_open_invoices(conn: sqlite3.Connection) -> list[dict]:
    return [_invoice_summary(conn, i) for i in repo.list_invoices_by_status(conn, "open")]


def list_history(conn: sqlite3.Connection, search: str | None = None) -> list[dict]:
    """Completed invoices grouped by month, newest month first."""
    rows = repo.list_invoices_by_status(conn, "completed")
    summaries = [_invoice_summary(conn, i) for i in rows]

    if search := _text(search, "Search"):
        q = search.lower()
        summaries = [
            s for s in summaries
            if q in s["customer"].lower()
            or q in str(s["no"])
            or q in (s["date"] or "").lower()
            or q in (s["plate"] or "").lower()
        ]

    groups: dict[str, dict] = {}
    for s in summaries:
        key = (s["date"] or "")[:7]  # YYYY-MM
        g = groups.setdefault(key, {"month": key, "label": _month_label(key),
                                    "invoices": [], "count": 0, "total": Decimal(0)})
        g["invoices"].append(s)
        g["count"] += 1
        g["total"] += money.dec(s["total"])

    ordered = sorted(groups.values(), key=lambda g: g["month"], reverse=True)
    for g in ordered:
        g["total"] = money.to_number(g["total"])
        g["invoices"].sort(key=lambda s: (s["date"] or "", s["no"]), reverse=True)
    return ordered


def duplicate_invoice(conn: sqlite3.Connection, no: int) -> dict:
    """Create a fresh open draft copying customer/vehicle/mileage/items."""
    src = repo.get_invoice(conn, _to_invoice_no(no))
    if not src:
        raise ValidationError("Invoice not found.")
    items = [
        {"name": r["name"], "qty": r["qty"], "price": float(money.money(r["price"])),
         "type": r["type"], "taxed": bool(r["taxed"])}
        for r in repo.get_invoice_items(conn, src["no"])
    ]
    # Keep the same mechanic if they are still on the roster; otherwise the
    # copy falls through to whoever is currently selected.
    src_mech = repo.get_mechanic(conn, src["mechanic_id"]) if src["mechanic_id"] else None
    saved = save_invoice(conn, {
        "customer_id": src["customer_id"],
        "vehicle_id": src["vehicle_id"],
        "mileage": src["mileage"],
        "comments": "",
        "items": items,
        "status": "open",
        "mechanic_id": src_mech["id"] if src_mech and src_mech["active"] else None,
        "tax_rate": float(invoice_tax_rate(conn, src)),
        "cc_fee_rate": float(invoice_cc_fee_rate(conn, src)),
    })
    return get_invoice(conn, saved["no"])


def delete_invoice(conn: sqlite3.Connection, no: int) -> dict:
    """Delete an OPEN invoice and its line items. Completed invoices are kept.

    Guarding on status protects finished invoice history from accidental loss.
    """
    inv = repo.get_invoice(conn, _to_invoice_no(no))
    if not inv:
        raise ValidationError("Invoice not found.")
    if inv["status"] != "open":
        raise ValidationError("Only open invoices can be deleted.")
    repo.delete_invoice(conn, inv["no"])
    return {"ok": True, "no": inv["no"]}


def dashboard_summary(conn: sqlite3.Connection) -> dict:
    today_str = today()
    completed = [_invoice_summary(conn, i) for i in repo.list_invoices_by_status(conn, "completed")]
    todays = [s for s in completed if s["date"] == today_str]
    revenue_today = money.to_number(sum((money.dec(s["total"]) for s in todays), Decimal(0)))
    recent = sorted(completed, key=lambda s: s["no"], reverse=True)[:5]
    open_list = list_open_invoices(conn)
    return {
        "invoices_today": len(todays),
        "revenue_today": revenue_today,
        "recent": recent,
        "open": open_list,
        "today": today_str,
    }

