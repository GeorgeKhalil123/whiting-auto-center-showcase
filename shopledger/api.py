"""JS ↔ Python bridge, usable headless.

In the desktop app the frontend calls `window.pywebview.api.<method>(...)` on
an instance of this class; the demo and tests call the same methods directly.
Every method returns a plain JSON-serializable value. Validation failures come back as
`{"ok": False, "error": "..."}` instead of throwing across the bridge; every
write runs in a transaction so a failure rolls back cleanly.

Money in returned payloads is already rounded to cents. Rates go in and come
out as fractions, the same as production: 0.06625 is 6.625% (labels such as
"6.625%" are returned alongside for display).
"""
from __future__ import annotations

import sqlite3
import threading
from os import PathLike
from pathlib import Path
from typing import Any, Callable

from . import db as db_module
from . import printing, services
from .services import ValidationError


def _data(data: object) -> dict:
    """The JSON object a write was sent. Anything else is a UI bug: report it
    as a validation error rather than an AttributeError deep in services."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError("Invalid data.")
    return data


class Api:
    def __init__(self, conn: sqlite3.Connection | None = None,
                 backup_dir: Path | str | None = None) -> None:
        # One shared connection (single-user desktop). A lock serializes the
        # JS-thread calls so transactions never interleave.
        self._conn = conn or db_module.init_db()
        self._lock = threading.RLock()
        # Back up whichever file this connection is on, not the default path —
        # the demo and tests run against temp databases.
        row = self._conn.execute("PRAGMA database_list").fetchone()
        self._db_file = Path(row["file"]) if row and row["file"] else None
        self._backup_dir = Path(backup_dir) if backup_dir else None

    # ---- transaction wrappers ------------------------------------------------

    def _read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            try:
                return fn(self._conn)
            except ValidationError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # pragma: no cover - defensive
                return {"ok": False, "error": f"Unexpected error: {exc}"}

    def _write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            try:
                result = fn(self._conn)
                self._conn.commit()
                return result
            except ValidationError as exc:
                self._conn.rollback()
                return {"ok": False, "error": str(exc)}
            except Exception as exc:  # pragma: no cover - defensive
                self._conn.rollback()
                return {"ok": False, "error": f"Unexpected error: {exc}"}

    # ---- bootstrap -----------------------------------------------------------

    def bootstrap(self) -> Any:
        """Full initial load in the frontend's state shape (one round-trip)."""
        return self._read(services.bootstrap)

    # ---- customers & vehicles -----------------------------------------------

    def list_customers(self, search: str | None = None) -> Any:
        return self._read(lambda c: services.list_customers(c, search))

    def get_customer(self, id: str) -> Any:
        return self._read(lambda c: services.get_customer(c, id))

    def create_customer(self, data: dict) -> Any:
        return self._write(lambda c: services.create_customer(c, _data(data)))

    def update_customer(self, id: str, data: dict) -> Any:
        return self._write(lambda c: services.update_customer(c, id, _data(data)))

    def delete_customer(self, id: str) -> Any:
        return self._write(lambda c: services.delete_customer(c, id))

    def add_vehicle(self, customer_id: str, data: dict) -> Any:
        return self._write(lambda c: services.add_vehicle(c, customer_id, _data(data)))

    def update_vehicle(self, id: str, data: dict) -> Any:
        return self._write(lambda c: services.update_vehicle(c, id, _data(data)))

    # ---- mechanics -----------------------------------------------------------

    def list_mechanics(self, include_inactive: bool = False) -> Any:
        return self._read(lambda c: services.list_mechanics(c, include_inactive))

    def create_mechanic(self, data: dict) -> Any:
        return self._write(lambda c: services.create_mechanic(c, _data(data)))

    def update_mechanic(self, id: str, data: dict) -> Any:
        return self._write(lambda c: services.update_mechanic(c, id, _data(data)))

    def delete_mechanic(self, id: str) -> Any:
        return self._write(lambda c: services.delete_mechanic(c, id))

    def set_active_mechanic(self, id: str) -> Any:
        """Switch the shop's current mechanic — the default on new invoices."""
        return self._write(lambda c: services.set_active_mechanic(c, id))

    # ---- catalog -------------------------------------------------------------

    def list_catalog(self, type: str, search: str | None = None, category: str | None = None) -> Any:
        return self._read(lambda c: services.list_catalog(c, type, search, category))

    def list_categories(self, type: str) -> Any:
        return self._read(lambda c: services.list_categories(c, type))

    def create_catalog_item(self, data: dict) -> Any:
        return self._write(lambda c: services.create_catalog_item(c, _data(data)))

    def update_catalog_item(self, id: str, data: dict) -> Any:
        return self._write(lambda c: services.update_catalog_item(c, id, _data(data)))

    def delete_catalog_item(self, id: str) -> Any:
        return self._write(lambda c: services.delete_catalog_item(c, id))

    # ---- invoices ------------------------------------------------------------

    def save_invoice(self, data: dict) -> Any:
        return self._write(lambda c: services.save_invoice(c, _data(data)))

    def get_invoice(self, no: int) -> Any:
        return self._read(lambda c: services.get_invoice(c, no))

    def list_open_invoices(self) -> Any:
        return self._read(services.list_open_invoices)

    def list_history(self, search: str | None = None) -> Any:
        return self._read(lambda c: services.list_history(c, search))

    def duplicate_invoice(self, no: int) -> Any:
        return self._write(lambda c: services.duplicate_invoice(c, no))

    def delete_invoice(self, no: int) -> Any:
        return self._write(lambda c: services.delete_invoice(c, no))

    def set_default_tax_rate(self, rate: float, tax_labor: bool | None = None) -> Any:
        """Change the shop-wide tax defaults. `rate` is a fraction: 0.06625 is 6.625%."""
        return self._write(lambda c: services.set_default_tax_rate(c, rate, tax_labor))

    def set_default_cc_fee(self, rate: float, on: bool | None = None) -> Any:
        """Change the shop-wide card-fee defaults. `rate` is a fraction: 0.03 is 3%."""
        return self._write(lambda c: services.set_default_cc_fee(c, rate, on))

    def dashboard_summary(self) -> Any:
        return self._read(services.dashboard_summary)

    # ---- printing ------------------------------------------------------------

    def print_invoice(self, no: int, dest: str | None = None, open_file: bool = True) -> Any:
        """Render the invoice to a PDF receipt and (by default) open it."""
        def run(conn: sqlite3.Connection) -> dict:
            invoice = services.get_invoice(conn, no)
            if dest is not None and not isinstance(dest, (str, PathLike)):
                raise ValidationError("Receipt path must be a file path.")
            try:
                path = printing.render(invoice, dest or None)
            except ValueError:   # e.g. a bare folder like "/" has no file name
                raise ValidationError("Receipt path must be a file path.") from None
            except OSError as exc:
                where = exc.filename or dest or "the temp folder"
                raise ValidationError(f"Can't write receipt to {where}: "
                                      f"{exc.strerror or exc}") from None
            if open_file:
                printing._open_file(path)
            return {"ok": True, "path": str(path)}

        return self._read(run)

    # ---- housekeeping --------------------------------------------------------

    def backup_now(self) -> Any:
        with self._lock:
            path = self._backup()
            return {"ok": True, "path": str(path) if path else None}

    def close(self) -> None:
        with self._lock:
            try:
                self._backup()
            finally:
                self._conn.close()

    def _backup(self) -> Path | None:
        if self._db_file is None:  # in-memory database: nothing on disk
            return None
        return db_module.backup(self._db_file, dest_dir=self._backup_dir)
