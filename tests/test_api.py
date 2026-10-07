"""The Api facade: one transaction per call, rollback on error, one lock.

New for the showcase — the production suite exercises services directly.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from shopledger import db as db_module
from shopledger import demo, printing, services
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
    no = api.save_invoice({"customer_id": cid, "cc_fee_rate": 0.03, "items": [
        {"name": "Brake Pad Set", "qty": 1, "price": 70, "type": "part"}]})["no"]
    res = api.print_invoice(no, dest=str(tmp_path / "receipt.pdf"), open_file=False)
    assert res["ok"], res
    path = Path(res["path"])
    assert path.suffix == ".pdf", f"fell back to {path.name} — is reportlab installed?"
    assert path.read_bytes().startswith(b"%PDF")
    assert printing.SHOP_NAME == "SAMPLE AUTO REPAIR"


def test_html_fallback_renders_the_same_totals(tmp_path, conn):
    api = Api(conn)
    cid = _new_customer(api)
    veh = api.add_vehicle(cid, {"make": "Honda", "model": "Civic", "plate": "DEMO-01"})
    api.update_customer(cid, {"phone": "555-0100"})
    no = api.save_invoice({"customer_id": cid, "vehicle_id": veh["id"], "items": [
        {"name": "Brake Pad Set", "qty": 1, "price": 100, "type": "part"}]})["no"]
    path = printing._render_html(api.get_invoice(no), tmp_path / "receipt")
    text = path.read_text(encoding="utf-8")
    assert "Tax (6.625%)" in text and "$106.63" in text
    # the same details grid as the PDF, phone and plate included
    assert "555-0100" in text and "DEMO-01" in text


def test_missing_reportlab_falls_back_loudly(tmp_path, conn, monkeypatch):
    def no_reportlab(*_a, **_k):
        raise ImportError("No module named 'reportlab'")
    monkeypatch.setattr(printing, "_render_pdf", no_reportlab)
    api = Api(conn)
    no = api.save_invoice({"customer_id": _new_customer(api), "items": [
        {"name": "Brake Pad Set", "qty": 1, "price": 100, "type": "part"}]})["no"]
    with pytest.warns(RuntimeWarning, match="HTML receipt instead"):
        path = printing.render(api.get_invoice(no), tmp_path / "receipt.pdf")
    assert path.suffix == ".html" and path.exists()


@pytest.mark.parametrize("call", ["get_invoice", "duplicate_invoice", "delete_invoice",
                                  "print_invoice"])
def test_bad_invoice_number_is_a_clean_error(conn, call):
    assert getattr(Api(conn), call)("abc") == {
        "ok": False, "error": "Invoice number must be a whole number."}


def test_bad_numbers_never_surface_as_unexpected_errors(conn):
    api = Api(conn)
    cid = _new_customer(api)
    results = [api.set_default_cc_fee("NaN"), api.set_default_tax_rate("Infinity")]
    for price, qty in (("NaN", 1), ("Infinity", 1), (80, 1.5), (1, 10**30)):
        results.append(api.save_invoice({"customer_id": cid, "items": [
            {"name": "Labor", "qty": qty, "price": price, "type": "labor"}]}))
    assert all(r["ok"] is False and "Unexpected" not in r["error"] for r in results), results


def test_demo_creates_a_missing_output_folder(tmp_path, capsys):
    out = tmp_path / "out" / "nested" / "x.pdf"
    assert demo.main(["--out", str(out)]) == 0
    assert out.read_bytes().startswith(b"%PDF")
    assert "backup" in capsys.readouterr().out


def test_demo_reports_an_unwritable_output_and_still_finishes(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    assert demo.main(["--out", str(blocker / "x.pdf")]) == 1
    printed = capsys.readouterr().out
    assert "receipt       FAILED: Can't write receipt to" in printed and "backup" in printed


@pytest.mark.parametrize("data", ["abc", 5, 1.5, True, ["x"]])
def test_non_object_data_is_invalid_not_unexpected(conn, data):
    api = Api(conn)
    cid = _new_customer(api)
    writes = [api.create_customer, api.create_mechanic, api.create_catalog_item,
              api.save_invoice, lambda d: api.add_vehicle(cid, d),
              lambda d: api.update_customer(cid, d)]
    for write in writes:
        assert write(data) == {"ok": False, "error": "Invalid data."}


@pytest.mark.parametrize("dest", [123, ["x"]])
def test_print_rejects_a_dest_that_is_not_a_path(conn, dest):
    api = Api(conn)
    no = api.save_invoice({"customer_id": _new_customer(api), "items": [
        {"name": "Part", "qty": 1, "price": 1, "type": "part"}]})["no"]
    assert api.print_invoice(no, dest=dest, open_file=False) == {
        "ok": False, "error": "Receipt path must be a file path."}


def test_print_to_an_unwritable_path_says_so(tmp_path, conn):
    api = Api(conn)
    no = api.save_invoice({"customer_id": _new_customer(api), "items": [
        {"name": "Part", "qty": 1, "price": 1, "type": "part"}]})["no"]
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    res = api.print_invoice(no, dest=str(blocker / "x.pdf"), open_file=False)
    assert res["ok"] is False
    assert res["error"].startswith("Can't write receipt to ") and "Unexpected" not in res["error"]


def test_demo_says_when_it_changes_the_extension(tmp_path, capsys):
    assert demo.main(["--out", str(tmp_path / "x.txt")]) == 0
    assert (tmp_path / "x.pdf").exists() and not (tmp_path / "x.txt").exists()
    assert "receipts are PDFs; writing x.pdf" in capsys.readouterr().out


def test_demo_writes_into_an_existing_folder(tmp_path, capsys):
    # "--out ../out" used to write a sibling ../out.pdf next to the folder.
    folder = tmp_path / "out"
    folder.mkdir()
    assert demo.main(["--out", str(folder)]) == 0
    assert (folder / "sample_invoice.pdf").read_bytes().startswith(b"%PDF")
    assert not (tmp_path / "out.pdf").exists()


def test_demo_out_root_never_tracebacks(capsys, monkeypatch):
    # "--out /" raised ValueError from with_suffix on an empty name. Stub the
    # write so the test never touches the real filesystem root.
    seen = {}

    def fake_print(self, no, dest=None, open_file=True):
        seen["dest"] = dest
        return {"ok": True, "path": dest}
    monkeypatch.setattr(Api, "print_invoice", fake_print)
    assert demo.main(["--out", "/"]) == 0
    assert Path(seen["dest"]) == Path("/").resolve() / "sample_invoice.pdf"
    assert "backup" in capsys.readouterr().out
