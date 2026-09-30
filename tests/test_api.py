"""The Api facade: one transaction per call, rollback on error, one lock.

New for the showcase — the production suite exercises services directly.
"""
from __future__ import annotations

import threading

from shopledger import db as db_module
from shopledger import printing, services
from shopledger.api import Api


def _counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
            db_module.get_setting(conn, "next_invoice_no"))


def _new_customer(api):
    cust = api.create_customer({"name": "Jane Sample"})
    return cust["id"]


def test_failed_write_rolls_back_every_statement(conn):
    api = Api(conn)
    before = _counts(conn)

    def half_done(c):
        ghost = services.create_customer(c, {"name": "Never Saved"})
        services.save_invoice(c, {"customer_id": ghost["id"], "items": [
            {"name": "Oil", "qty": 1, "price": 40, "type": "labor"}]})
        raise RuntimeError("boom")

    res = api._write(half_done)
    assert res == {"ok": False, "error": "Unexpected error: boom"}
    assert _counts(conn) == before            # customer, invoice, counter all undone


def test_validation_error_comes_back_as_data(conn):
    api = Api(conn)
    cid = _new_customer(api)
    res = api.save_invoice({"customer_id": cid, "items": []})
    assert res == {"ok": False, "error": "Add at least one item before saving."}
    assert api.get_invoice(99999) == {"ok": False, "error": "Invoice not found."}


def test_successful_write_is_committed(db_file, conn):
    api = Api(conn)
    cid = _new_customer(api)
    no = api.save_invoice({"customer_id": cid, "items": [
        {"name": "Oil", "qty": 1, "price": 40, "type": "labor"}]})["no"]
    other = db_module.connect(db_file)        # a second connection sees it
    assert other.execute("SELECT COUNT(*) FROM invoices WHERE no = ?", (no,)).fetchone()[0] == 1
    other.close()


def test_concurrent_saves_never_reuse_a_number(conn):
    api = Api(conn)
    cid = _new_customer(api)
    numbers, errors = [], []

    def worker():
        for _ in range(5):
            r = api.save_invoice({"customer_id": cid, "items": [
                {"name": "Oil", "qty": 1, "price": 40, "type": "labor"}]})
            (numbers if "no" in r else errors).append(r.get("no", r))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(numbers) == list(range(1000, 1040))


def test_backup_copies_the_live_db_and_prunes(tmp_path, conn):
    api = Api(conn, backup_dir=tmp_path / "backups")
    _new_customer(api)
    first = api.backup_now()["path"]
    restored = db_module.connect(first)
    assert restored.execute("SELECT name FROM customers").fetchone()[0] == "Jane Sample"
    restored.close()

    for _ in range(12):
        db_module.backup(api._db_file, keep=10, dest_dir=tmp_path / "backups")
    assert len(list((tmp_path / "backups").glob("shop-*.db"))) == 10


def test_print_invoice_writes_a_pdf(tmp_path, conn):
    api = Api(conn)
    cid = _new_customer(api)
    no = api.save_invoice({"customer_id": cid, "cc_fee_rate": 3, "items": [
        {"name": "Brake Pad Set", "qty": 1, "price": 70, "type": "part"}]})["no"]
    res = api.print_invoice(no, dest=str(tmp_path / "receipt.pdf"), open_file=False)
    data = (tmp_path / "receipt.pdf").read_bytes()
    assert res["ok"] and data.startswith(b"%PDF")
    assert printing.SHOP_NAME == "SAMPLE AUTO REPAIR"


def test_html_fallback_renders_the_same_totals(tmp_path, conn):
    api = Api(conn)
    cid = _new_customer(api)
    no = api.save_invoice({"customer_id": cid, "items": [
        {"name": "Brake Pad Set", "qty": 1, "price": 100, "type": "part"}]})["no"]
    path = printing._render_html(api.get_invoice(no), tmp_path / "receipt")
    text = path.read_text(encoding="utf-8")
    assert "Tax (6.625%)" in text and "$106.63" in text
