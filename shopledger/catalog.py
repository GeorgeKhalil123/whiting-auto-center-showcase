"""Labor/parts price list — the catalog the invoice editor picks lines from.

Split out of services.py to keep it short; services re-exports all of it.
"""
from __future__ import annotations

import sqlite3

from . import repository as repo
from .rules import ValidationError, _require_type, _text, _to_id, _to_price
from .shaping import _catalog_dict


def list_catalog(conn: sqlite3.Connection, type_: str, search: str | None = None,
                 category: str | None = None) -> list[dict]:
    _require_type(type_)
    return [_catalog_dict(r) for r in repo.list_catalog(
        conn, type_, _text(search, "Search"), _text(category, "Category"))]


def list_categories(conn: sqlite3.Connection, type_: str) -> list[str]:
    _require_type(type_)
    return repo.list_categories(conn, type_)


def create_catalog_item(conn: sqlite3.Connection, data: dict) -> dict:
    type_ = data.get("type")
    _require_type(type_)
    name = _text(data.get("name"), "Item name")
    if not name:
        raise ValidationError("Item name is required.")
    category = _text(data.get("category"), "Category") or "General"
    desc = _text(data.get("desc"), "Description")
    price = _to_price(data.get("price"))
    cid = repo.create_catalog_item(conn, type_, name, price, category, desc)
    return _catalog_dict(repo.get_catalog_item(conn, cid))


def update_catalog_item(conn: sqlite3.Connection, cid: str, data: dict) -> dict:
    if not repo.get_catalog_item(conn, cid := _to_id(cid, "Catalog item")):
        raise ValidationError("Catalog item not found.")
    fields: dict = {}
    if "type" in data:
        _require_type(data["type"])
        fields["type"] = data["type"]
    if "name" in data:
        if not (name := _text(data["name"], "Item name")):
            raise ValidationError("Item name is required.")
        fields["name"] = name
    if "price" in data:
        fields["price"] = _to_price(data["price"])
    if "category" in data:
        fields["category"] = _text(data["category"], "Category") or "General"
    if "desc" in data:
        fields["desc"] = _text(data["desc"], "Description")
    repo.update_catalog_item(conn, cid, fields)
    return _catalog_dict(repo.get_catalog_item(conn, cid))


def delete_catalog_item(conn: sqlite3.Connection, cid: str) -> dict:
    if not repo.get_catalog_item(conn, cid := _to_id(cid, "Catalog item")):
        raise ValidationError("Catalog item not found.")
    repo.delete_catalog_item(conn, cid)
    return {"ok": True}
