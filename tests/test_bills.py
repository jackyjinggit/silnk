"""未支付账单落库：重启不丢、过期清理、幂等读取。"""

import time

import db


def _bill(out_trade_no, **extra):
    bill = {"out_trade_no": out_trade_no, "directory": "S1",
            "total_price": 1.2, "created_ts": time.time(),
            "expiry": time.time() + 3600, "files_attributes": []}
    bill.update(extra)
    return bill


def test_save_and_get_bill(temp_db):
    db.save_bill("B1", _bill("B1"))
    loaded = db.get_bill("B1")
    assert loaded["total_price"] == 1.2
    assert loaded["directory"] == "S1"


def test_bill_survives_new_connection(temp_db):
    # 模拟进程重启：同一数据库文件，新的查询连接仍能读到
    db.save_bill("B2", _bill("B2"))
    assert [no for no, _ in db.list_bills()] == ["B2"]


def test_expired_bill_is_removed(temp_db):
    db.save_bill("B3", _bill("B3", expiry=time.time() - 1))
    assert db.get_bill("B3") is None
    assert db.list_bills() == []


def test_delete_bill(temp_db):
    db.save_bill("B4", _bill("B4"))
    assert db.delete_bill("B4") == 1
    assert db.get_bill("B4") is None


def test_clear_expired_bills(temp_db):
    db.save_bill("B5", _bill("B5"))
    db.save_bill("B6", _bill("B6", expiry=time.time() - 1))
    assert db.clear_expired_bills() >= 1
    assert [no for no, _ in db.list_bills()] == ["B5"]


def test_duplicate_out_trade_no_overwrites(temp_db):
    db.save_bill("B7", _bill("B7", total_price=1.0))
    db.save_bill("B7", _bill("B7", total_price=2.0))
    assert db.get_bill("B7")["total_price"] == 2.0
    assert len(db.list_bills()) == 1
