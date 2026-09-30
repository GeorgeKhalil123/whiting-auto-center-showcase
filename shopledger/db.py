"""SQLite connection, schema, migrations, and backups.

Single local file (see paths.db_path). Foreign keys on, WAL for durability,
a `settings` key/value table holding tax_rate / next_invoice_no / schema_version.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from . import paths

SCHEMA_VERSION = 6

# The name the pre-v3 app hardcoded as its only technician. Invoices from that
# era carry it in `serviced_by`; the v3 backfill keeps its old "Owner" label.
LEGACY_TECH_NAME = "Sample Tech"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS customers (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    phone    TEXT,
    since    TEXT,
    notes    TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vehicles (
    id          TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    year        INTEGER,
    make        TEXT,
    model       TEXT,
    plate       TEXT,
    vin         TEXT,
    mileage     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_vehicles_customer ON vehicles(customer_id);

CREATE TABLE IF NOT EXISTS catalog_items (
    id       TEXT PRIMARY KEY,
    type     TEXT NOT NULL,
    name     TEXT NOT NULL,
    price    NUMERIC NOT NULL,
    category TEXT,
    desc     TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_type     ON catalog_items(type);
CREATE INDEX IF NOT EXISTS idx_catalog_name     ON catalog_items(name);
CREATE INDEX IF NOT EXISTS idx_catalog_category ON catalog_items(category);

CREATE TABLE IF NOT EXISTS mechanics (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT,
    phone      TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mechanics_active ON mechanics(active);

CREATE TABLE IF NOT EXISTS invoices (
    no          INTEGER PRIMARY KEY,
    customer_id TEXT REFERENCES customers(id),
    vehicle_id  TEXT REFERENCES vehicles(id),
    date        TEXT,
    mileage     INTEGER,
    status      TEXT NOT NULL,
    comments    TEXT,
    mechanic_id TEXT REFERENCES mechanics(id),
    serviced_by TEXT,
    -- Rate this invoice was written at, as a fraction (0.06625 = 6.625%).
    -- NULL on pre-v4 rows: those fall back to the shop default in settings.
    tax_rate    NUMERIC,
    -- Card surcharge as a fraction (0.03 = 3%). 0 / NULL means none was
    -- charged — unlike tax, this never falls back to the shop default, because
    -- the fee only applies when the customer actually paid by card.
    cc_fee_rate NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_invoices_status   ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date     ON invoices(date);
-- idx_invoices_mechanic is created in _migrate: on a pre-v3 database the
-- invoices table already exists without the column, so the index has to wait
-- until after the ALTER TABLE.

CREATE TABLE IF NOT EXISTS invoice_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no INTEGER NOT NULL REFERENCES invoices(no) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    price      NUMERIC NOT NULL,
    type       TEXT NOT NULL,
    taxed      INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER
);
CREATE INDEX IF NOT EXISTS idx_invoice_items_no ON invoice_items(invoice_no);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with sane pragmas and Row access."""
    target = str(path or paths.db_path())
    # check_same_thread=False: pywebview dispatches JS bridge calls (and the
    # window-close callback) on threads other than the one that opened the DB.
    # Safe here because api.Api serializes every access behind a single RLock.
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _is_empty(conn: sqlite3.Connection) -> bool:
    """True only on a brand-new database, so seeding never re-runs.

    Keyed off the two tables seed.py fills (catalog + mechanics). Customers and
    invoices are no longer seeded, so they can't be used as the signal — a real
    shop's first launch has none of either.
    """
    cat = conn.execute("SELECT COUNT(*) AS n FROM catalog_items").fetchone()
    mech = conn.execute("SELECT COUNT(*) AS n FROM mechanics").fetchone()
    return cat["n"] == 0 and mech["n"] == 0


def init_db(path: Path | None = None) -> sqlite3.Connection:
    """Create the schema, run migrations, and seed on first launch.

    Idempotent: safe to call on every boot.
    """
    conn = connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()

    if get_setting(conn, "schema_version") is None:
        set_setting(conn, "schema_version", str(SCHEMA_VERSION))
    if get_setting(conn, "tax_rate") is None:
        set_setting(conn, "tax_rate", "0.06625")
    if get_setting(conn, "tax_labor") is None:
        set_setting(conn, "tax_labor", "0")  # parts are taxed, labor is not
    if get_setting(conn, "cc_fee_rate") is None:
        set_setting(conn, "cc_fee_rate", "0.03")
    if get_setting(conn, "cc_fee_on") is None:
        # Off by default: only card payments get surcharged, so charging every
        # invoice by default would quietly overcharge anyone paying cash.
        set_setting(conn, "cc_fee_on", "0")

    if _is_empty(conn):
        from . import seed  # local import to avoid a cycle

        seed.seed(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only migrations keyed off the schema_version setting.

    v0 -> v1 is the initial schema (created above). Future column additions
    append a numbered block here and bump SCHEMA_VERSION; data is never wiped.
    """
    current = get_setting(conn, "schema_version")
    version = int(current) if current is not None else 0
    if version < 2:
        # Soft-delete flag so a customer with completed-invoice history can be
        # removed from active lists without deleting those invoices. Guard the
        # ALTER: fresh DBs already have the column from SCHEMA above.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(customers)")}
        if "archived" not in cols:
            conn.execute("ALTER TABLE customers ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        set_setting(conn, "schema_version", "2")
    if version < 3:
        # Mechanics moved from a hardcoded name to their own table. The
        # `mechanics` table itself comes from SCHEMA above; here we add the
        # invoice link column and turn existing `serviced_by` names into rows.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
        if "mechanic_id" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN mechanic_id TEXT REFERENCES mechanics(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_mechanic ON invoices(mechanic_id)")
        _backfill_mechanics(conn)
        set_setting(conn, "schema_version", "3")
    if version < 4:
        # Tax rate became per-invoice: the shop default in `settings` seeds new
        # invoices, but each one keeps the rate it was written at so history
        # reprints with the same numbers after the default changes. Existing
        # invoices are backfilled with the current default.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
        if "tax_rate" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN tax_rate NUMERIC")
        conn.execute("UPDATE invoices SET tax_rate = ? WHERE tax_rate IS NULL",
                     (get_setting(conn, "tax_rate", "0.06625"),))
        set_setting(conn, "schema_version", "4")
    if version < 5:
        # Optional card surcharge, stored per invoice like the tax rate.
        # Existing invoices never had one, so they backfill to 0 rather than to
        # the shop default — nobody's history should grow a fee retroactively.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(invoices)")}
        if "cc_fee_rate" not in cols:
            conn.execute("ALTER TABLE invoices ADD COLUMN cc_fee_rate NUMERIC")
        conn.execute("UPDATE invoices SET cc_fee_rate = 0 WHERE cc_fee_rate IS NULL")
        set_setting(conn, "schema_version", "5")
    if version < 6:
        # v4/v5 froze every pre-existing invoice's tax_rate/cc_fee_rate at a
        # snapshot value, including ones still open. A closed invoice is a
        # finished sale and must stay frozen, but an open one hasn't closed
        # out yet — it should keep tracking the shop's live tax/fee settings
        # the same way a brand-new invoice would, right up until it's
        # completed. Clear the snapshot back to NULL only for open invoices
        # so invoice_tax_rate / invoice_cc_fee_rate fall through to the live
        # default again; completed invoices are left untouched.
        conn.execute("UPDATE invoices SET tax_rate = NULL, cc_fee_rate = NULL "
                     "WHERE status = 'open'")
        set_setting(conn, "schema_version", "6")
    if version < SCHEMA_VERSION:
        set_setting(conn, "schema_version", str(SCHEMA_VERSION))


def _backfill_mechanics(conn: sqlite3.Connection) -> None:
    """Turn each distinct pre-v3 `serviced_by` name into a mechanics row.

    Existing invoices then point at the matching mechanic, so the roster starts
    out matching whatever history is already on disk. A fresh database has no
    invoices yet — seed.py installs the starter roster instead.
    """
    names = [r["serviced_by"] for r in conn.execute(
        "SELECT DISTINCT serviced_by FROM invoices "
        "WHERE serviced_by IS NOT NULL AND TRIM(serviced_by) != ''"
    )]
    for order, name in enumerate(sorted(names)):
        row = conn.execute(
            "SELECT id FROM mechanics WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row:
            mid = row["id"]
        else:
            mid = f"m{uuid.uuid4().hex[:12]}"
            # LEGACY_TECH_NAME was the app's hardcoded technician; keep the old
            # sidebar label ("Owner") for that one, everyone else starts a
            # Technician.
            is_legacy = name.strip().lower() == LEGACY_TECH_NAME.lower()
            role = "Owner" if is_legacy else "Technician"
            conn.execute(
                "INSERT INTO mechanics(id, name, role, active, sort_order) VALUES (?, ?, ?, 1, ?)",
                (mid, name, role, order),
            )
        conn.execute(
            "UPDATE invoices SET mechanic_id = ? "
            "WHERE mechanic_id IS NULL AND lower(serviced_by) = lower(?)",
            (mid, name),
        )

    if get_setting(conn, "active_mechanic_id") is None:
        first = conn.execute(
            "SELECT id FROM mechanics WHERE active = 1 ORDER BY sort_order, name COLLATE NOCASE"
        ).fetchone()
        if first:
            set_setting(conn, "active_mechanic_id", first["id"])


def backup(path: Path | None = None, keep: int = 10,
           dest_dir: Path | None = None) -> Path | None:
    """Copy the DB to a timestamped file in backups/, pruning to the last N.

    Uses SQLite's online backup API so it is safe while the DB is open.
    Returns the backup path, or None if there is no DB yet.
    """
    src = path or paths.db_path()
    if not Path(src).exists():
        return None
    folder = Path(dest_dir) if dest_dir else paths.backups_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # Microseconds in the name: two backups in the same second (window closed
    # right after a manual backup) must not overwrite each other.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = folder / f"shop-{stamp}.db"

    source = connect(src)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    _prune_backups(folder, keep)
    return dest


def _prune_backups(folder: Path, keep: int) -> None:
    files = sorted(folder.glob("shop-*.db"))
    for old in files[:-keep] if keep > 0 else files:
        try:
            old.unlink()
        except OSError:
            pass
