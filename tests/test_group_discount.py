"""拼单折扣阶梯：人数 → 折扣系数，以及「还差几人」提示。"""

import db
from routers import group as group_router
from routers.group import discount_for, next_tier


def _touch_member_files(tmp_path, monkeypatch, group_no, keys):
    """拼单锁定会校验成员文件是否存在：测试里落几个占位 PDF。"""
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "uploads" / f"group_{group_no}" / "converted"
    folder.mkdir(parents=True)
    for key in keys:
        (folder / f"{key}.pdf").write_bytes(b"%PDF-1.4\n%%EOF")


def test_discount_ladder_boundaries():
    assert discount_for(1) == 1.0
    assert discount_for(2) == 1.0
    assert discount_for(3) == 0.9
    assert discount_for(4) == 0.9
    assert discount_for(5) == 0.8
    assert discount_for(9) == 0.8
    assert discount_for(10) == 0.7
    assert discount_for(99) == 0.7


def test_next_tier_hint():
    assert next_tier(1) == {"need": 3, "factor": 0.9, "more": 2}
    assert next_tier(3) == {"need": 5, "factor": 0.8, "more": 2}
    assert next_tier(5) == {"need": 10, "factor": 0.7, "more": 5}
    assert next_tier(10) is None


def _add_member(group_no, name, amount, key, uid, pages=2):
    db.add_group_member(group_no, name, key, f"{key}.pdf", 1, 1, pages,
                        "one-sided", "bw", "A4", True, pages, amount,
                        0.0, [], user_id=uid)


def test_aa_collect_creates_one_bill_per_member(temp_db, tmp_path, monkeypatch):
    db.create_group("G1", "发起人", "owner-key", pay_mode="aa")
    _add_member("G1", "张三", 10.0, "k1", 1)
    _add_member("G1", "李四", 5.0, "k2", 2)
    _add_member("G1", "王五", 5.0, "k3", 3)
    _touch_member_files(tmp_path, monkeypatch, "G1", ["k1", "k2", "k3"])

    result = group_router.group_collect("G1", {"token": "owner-key", "uid": 9})
    assert result.message == "success"
    data = result.data
    assert data["pay_mode"] == "aa"
    assert data["status"] == "collecting"
    assert data["member_count"] == 3
    assert data["paid_count"] == 0
    # 3 人 9 折：原价 20 → 18，按人分摊且合计不能丢分
    assert data["total_price"] == 18.0
    assert round(sum(m["payable"] for m in data["members"]), 2) == 18.0
    for member in db.list_group_members("G1"):
        assert member["out_trade_no"]
        assert member["pickup_code"]
        assert db.get_bill(member["out_trade_no"]) is not None


def test_aa_member_payment_marks_paid_and_finishes_group(temp_db, tmp_path, monkeypatch):
    db.create_group("G2", "发起人", "owner-key", pay_mode="aa")
    _add_member("G2", "张三", 10.0, "k1", 1)
    _add_member("G2", "李四", 10.0, "k2", 2)
    _touch_member_files(tmp_path, monkeypatch, "G2", ["k1", "k2"])
    group_router.group_collect("G2", {"token": "owner-key", "uid": 9})
    members = db.list_group_members("G2")

    assert db.mark_group_member_paid(members[0]["out_trade_no"]) == "G2"
    assert db.refresh_group_paid_status("G2") == "collecting"
    # 换会话后仍能按账号识别「我的账单」
    mine = group_router._group_state(db.get_group("G2"), "new-session", 1)
    assert mine["my_bill"]["paid"] is True
    assert mine["my_bill"]["pickup_code"]

    assert db.mark_group_member_paid(members[1]["out_trade_no"]) == "G2"
    assert db.refresh_group_paid_status("G2") == "paid"


def test_unified_group_rejects_aa_collect(temp_db):
    db.create_group("G3", "发起人", "owner-key", pay_mode="unified")
    _add_member("G3", "张三", 10.0, "k1", 1)
    result = group_router.group_collect("G3", {"token": "owner-key", "uid": 9})
    assert result.message != "success"
    assert "统一付款" in result.message
