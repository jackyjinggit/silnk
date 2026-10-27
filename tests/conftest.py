import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db  # noqa: E402


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """每个用例独立的临时 SQLite 库，避免污染 printer.db。"""
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    return db
