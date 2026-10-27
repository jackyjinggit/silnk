"""数据库每日备份：生成文件、保留最近 N 份。"""

import os

import db
from automatic_tasks import backup_database


def test_backup_creates_file(temp_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db.add_job({"folder": "F", "filename": "a.pdf", "print_copies": 1,
                "print_range_start": 1, "print_range_end": 1,
                "print_side": "one-sided", "display_name": "a.pdf"})
    name = backup_database()
    assert name and os.path.isfile(name)
    # 备份里能读到刚写入的订单
    import sqlite3
    conn = sqlite3.connect(name)
    try:
        count = conn.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_backup_keeps_latest_files(temp_db, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("backups", exist_ok=True)
    for day in ("20260101", "20260102", "20260103"):
        with open(f"backups/printer-{day}.db", "wb") as f:
            f.write(b"old")
    backup_database(keep=2)
    files = sorted(os.listdir("backups"))
    assert len(files) == 2
    assert files[-1].startswith("printer-")
