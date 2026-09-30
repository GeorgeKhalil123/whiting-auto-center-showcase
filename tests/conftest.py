"""Shared fixtures: every test gets its own on-disk temp DB, never a real one."""
from __future__ import annotations

import pytest

from shopledger import db as db_module


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # Default paths (backups, db_path) resolve under the test's temp dir.
    monkeypatch.setenv("SHOPLEDGER_HOME", str(tmp_path / "home"))


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "shop.db"


@pytest.fixture
def conn(db_file):
    """A fresh database: schema, migrations, toy seed."""
    c = db_module.init_db(db_file)
    yield c
    c.close()
