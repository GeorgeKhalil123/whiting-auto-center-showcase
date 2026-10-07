"""Mechanic roster — who can be on an invoice, and who new invoices default to."""
from __future__ import annotations

import sqlite3

from . import repository as repo
from .db import get_setting, set_setting
from .rules import ValidationError, _given, _text, _to_id
from .shaping import _mechanic_dict


def list_mechanics(conn: sqlite3.Connection, include_inactive: bool = False) -> list[dict]:
    return [_mechanic_dict(r) for r in repo.list_mechanics(conn, include_inactive)]


def active_mechanic_id(conn: sqlite3.Connection) -> str | None:
    """The shop's currently selected mechanic — the default on new invoices.

    Self-heals if the stored id points at a mechanic who was since removed or
    deactivated: falls back to the first one on the roster.
    """
    stored = get_setting(conn, "active_mechanic_id")
    if stored:
        row = repo.get_mechanic(conn, stored)
        if row and row["active"]:
            return stored
    roster = repo.list_mechanics(conn)
    if not roster:
        return None
    set_setting(conn, "active_mechanic_id", roster[0]["id"])
    return roster[0]["id"]


def set_active_mechanic(conn: sqlite3.Connection, mid: str) -> dict:
    row = repo.get_mechanic(conn, mid := _to_id(mid, "Mechanic"))
    if not row:
        raise ValidationError("Mechanic not found.")
    if not row["active"]:
        raise ValidationError("That mechanic is no longer on the roster.")
    set_setting(conn, "active_mechanic_id", mid)
    return {"ok": True, "activeMechanicId": mid}


def create_mechanic(conn: sqlite3.Connection, data: dict) -> dict:
    name = _text(data.get("name"), "Mechanic name")
    if not name:
        raise ValidationError("Mechanic name is required.")
    existing = repo.find_mechanic_by_name(conn, name)
    if existing and existing["active"]:
        raise ValidationError(f"“{name}” is already on the roster.")
    role = _text(data.get("role"), "Role")
    phone = _text(data.get("phone"), "Phone")
    if existing:  # same name, previously removed — bring them back
        repo.update_mechanic(conn, existing["id"], {"active": 1, "role": role, "phone": phone})
        mid = existing["id"]
    else:
        mid = repo.create_mechanic(conn, name, role, phone)
    if active_mechanic_id(conn) is None:
        set_setting(conn, "active_mechanic_id", mid)
    return _mechanic_dict(repo.get_mechanic(conn, mid))


def update_mechanic(conn: sqlite3.Connection, mid: str, data: dict) -> dict:
    row = repo.get_mechanic(conn, mid := _to_id(mid, "Mechanic"))
    if not row:
        raise ValidationError("Mechanic not found.")
    fields: dict = {}
    if "name" in data:
        name = _text(data["name"], "Mechanic name")
        if not name:
            raise ValidationError("Mechanic name is required.")
        clash = repo.find_mechanic_by_name(conn, name)
        if clash and clash["id"] != mid:
            raise ValidationError(f"“{name}” is already on the roster.")
        fields["name"] = name
    if "role" in data:
        fields["role"] = _text(data["role"], "Role")
    if "phone" in data:
        fields["phone"] = _text(data["phone"], "Phone")
    repo.update_mechanic(conn, mid, fields)
    if fields.get("name") and fields["name"] != row["name"]:
        repo.rename_invoice_mechanic(conn, mid, fields["name"])
    return _mechanic_dict(repo.get_mechanic(conn, mid))


def delete_mechanic(conn: sqlite3.Connection, mid: str) -> dict:
    """Take a mechanic off the roster.

    Mirrors customer deletion: if they have invoices the row is deactivated
    (kept for history), otherwise it is deleted outright. The last remaining
    mechanic can't be removed — invoices need someone to be serviced by.
    """
    row = repo.get_mechanic(conn, mid := _to_id(mid, "Mechanic"))
    if not row:
        raise ValidationError("Mechanic not found.")
    roster = repo.list_mechanics(conn)
    if len(roster) <= 1:
        raise ValidationError("Add another mechanic before removing the last one.")

    count = repo.count_invoices_by_mechanic(conn, mid)
    if count:
        repo.deactivate_mechanic(conn, mid)
    else:
        repo.delete_mechanic(conn, mid)

    if get_setting(conn, "active_mechanic_id") == mid:
        set_setting(conn, "active_mechanic_id", "")
    return {"ok": True, "id": mid, "deactivated": bool(count),
            "activeMechanicId": active_mechanic_id(conn)}


def _resolve_mechanic(conn: sqlite3.Connection, data: dict,
                      existing: sqlite3.Row | None) -> tuple[str | None, str]:
    """(mechanic_id, name) for a save: explicit pick, else the invoice's own,
    else the shop's active mechanic."""
    mid = _given(data, "mechanic_id", "mechanicId")
    if mid is not None:
        row = repo.get_mechanic(conn, _to_id(mid, "Mechanic"))
        if not row:
            raise ValidationError("Mechanic not found.")
        return row["id"], row["name"]
    if existing is not None and existing["mechanic_id"]:
        row = repo.get_mechanic(conn, existing["mechanic_id"])
        if row:
            return row["id"], row["name"]
    fallback = active_mechanic_id(conn)
    if fallback:
        row = repo.get_mechanic(conn, fallback)
        if row:
            return row["id"], row["name"]
    # No roster at all (every mechanic removed from an old DB): keep whatever
    # name the invoice already carried rather than dropping it.
    return None, (existing["serviced_by"] if existing is not None else "") or ""

