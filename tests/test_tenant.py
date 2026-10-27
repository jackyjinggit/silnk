"""多租户（软件订阅线）最小骨架测试。

覆盖：开通租户、每租户密钥校验、订阅维护、租户感知计价三级生效、
设备/订单/告警的租户归属与过滤。
"""

import db
from jwt import role_of_key, tenant_of_key
from routers import tenant as tenant_router


def test_create_tenant_generates_secret_and_subscription(temp_db):
    result = db.create_tenant("t1", name="德科文印室", printer_limit=2)
    assert result is not None
    assert result["tenant_id"] == "t1"
    assert result["status"] == "active"
    # 未指定密钥时自动生成，且非空
    assert result["secret_key"].startswith("sk_")
    subscription = result["subscription"]
    assert subscription["tenant_id"] == "t1"
    assert subscription["printer_limit"] == 2
    assert subscription["status"] == "active"


def test_create_tenant_is_idempotent_guard(temp_db):
    assert db.create_tenant("t1") is not None
    # 重复开通同一 tenant_id 应被拦下
    assert db.create_tenant("t1") is None


def test_default_tenant_seeded_on_init(temp_db):
    # init_db 应自动建出默认租户，历史数据归位
    default = db.get_tenant("default")
    assert default is not None
    assert default["status"] == "active"
    assert default["subscription"]["plan"] == "platform"


def test_get_tenant_by_secret_and_wrong_key(temp_db):
    created = db.create_tenant("t1", secret_key="tenant-key-abc")
    assert db.get_tenant_by_secret("tenant-key-abc")["tenant_id"] == "t1"
    assert db.get_tenant_by_secret("wrong-key") is None
    assert db.get_tenant_by_secret("") is None
    # 默认租户密钥 = 全局 SECRET_KEY，反查应命中 default
    import setting
    assert db.get_tenant_by_secret(setting.SECRET_KEY)["tenant_id"] == "default"


def test_suspended_tenant_key_rejected(temp_db):
    db.create_tenant("t1", secret_key="tenant-key-abc")
    db.set_tenant_status("t1", "suspended")
    assert db.get_tenant_by_secret("tenant-key-abc") is None
    db.set_tenant_status("t1", "active")
    assert db.get_tenant_by_secret("tenant-key-abc") is not None


def test_role_of_key_rejects_tenant_key(temp_db):
    db.create_tenant("t1", secret_key="tenant-key-abc")
    # 租户密钥不属于平台角色，避免越权访问平台级接口
    assert role_of_key("tenant-key-abc") is None
    assert tenant_of_key("tenant-key-abc")["tenant_id"] == "t1"


def test_subscription_update(temp_db):
    db.create_tenant("t1", printer_limit=1)
    updated = db.set_subscription("t1", plan="pro", printer_limit=5,
                                  expires_at="2030-01-01")
    assert updated["plan"] == "pro"
    assert updated["printer_limit"] == 5
    assert updated["expires_at"] == "2030-01-01"
    # 未传的字段保持原值
    again = db.set_subscription("t1", plan="team")
    assert again["plan"] == "team"
    assert again["printer_limit"] == 5
    # 不存在的租户返回 None
    assert db.set_subscription("nope", plan="x") is None


def test_effective_price_tiers_device_over_tenant_over_global(temp_db):
    db.create_tenant("t1")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    # 无租户价时跟随全局价
    base = db.effective_price("d1")
    assert base["source"] == "global"
    assert base["tenant_id"] == "t1"
    # 设租户默认价 → 生效来源 tenant
    db.set_tenant_price("t1", bw=1.0, color=2.0, bw_duplex=1.5, color_duplex=3.0)
    tenant_priced = db.effective_price("d1")
    assert tenant_priced["source"] == "tenant"
    assert tenant_priced["bw"] == 1.0
    assert tenant_priced["color"] == 2.0
    # 设备独立价优先于租户价
    db.set_device_price("d1", bw=9.9)
    device_priced = db.effective_price("d1")
    assert device_priced["source"] == "device"
    assert device_priced["bw"] == 9.9
    # 其余项仍来自租户价
    assert device_priced["color"] == 2.0
    # 清除设备价 → 回到租户价
    db.clear_device_price("d1")
    assert db.effective_price("d1")["source"] == "tenant"


def test_other_tenant_price_does_not_leak(temp_db):
    db.create_tenant("t1")
    db.create_tenant("t2")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    db.set_tenant_price("t2", bw=8.8)
    # t2 的租户价不应影响属于 t1 的设备
    price = db.effective_price("d1")
    assert price["source"] == "global"
    assert price["bw"] != 8.8


def test_device_and_order_carry_tenant(temp_db):
    db.create_tenant("t1")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    assert db.get_device("d1")["tenant_id"] == "t1"
    db.add_job({"folder": "f", "filename": "a.pdf", "print_copies": 1,
                "print_range_start": 1, "print_range_end": 1,
                "print_side": "one-sided"}, printer_id="d1")
    jobs = db.list_jobs("t1")
    assert len(jobs) == 1
    assert jobs[0]["tenant_id"] == "t1"
    # 其他租户看不到
    assert db.list_jobs("t2") == []


def test_list_devices_filtered_by_tenant(temp_db):
    db.create_tenant("t1")
    db.create_tenant("t2")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    db.register_device("d2", tenant_id="t2", paper_capacity=100)
    assert {d["device_id"] for d in db.list_devices("t1")} == {"d1"}
    assert {d["device_id"] for d in db.list_devices("t2")} == {"d2"}
    assert {d["device_id"] for d in db.list_devices()} == {"d1", "d2"}


def test_alert_carries_device_tenant(temp_db):
    db.create_tenant("t1")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    db.add_alert("R1", "d1", "warning", "纸量不足")
    assert len(db.list_alerts(tenant_id="t1")) == 1
    assert db.list_alerts(tenant_id="t2") == []


def test_tenant_self_reports_own_price_and_global_fallback(temp_db):
    """租户控制台首页数据：自设价 + 平台全局价都要如实返回。

    price 为 None 表示本租户没单独定过价（跟随平台全局价）；
    改本租户价只写 tenant_price，不动平台全局价。
    """
    db.create_tenant("t1", secret_key="key-t1")
    info = tenant_router.tenant_self(tenant=db.get_tenant_by_secret("key-t1"))
    assert info.message == "success"
    assert info.data["price"] is None
    global_bw = info.data["global_price"]["bw"]
    assert global_bw > 0

    db.set_tenant_price("t1", bw=1.5)
    again = tenant_router.tenant_self(tenant=db.get_tenant_by_secret("key-t1"))
    assert again.data["price"]["bw"] == 1.5
    # 租户自设价不影响平台全局价
    assert again.data["global_price"]["bw"] == global_bw


def test_tenant_self_price_follows_tenant_only(temp_db):
    """租户自助改价只作用于本租户设备，其他租户不受影响。"""
    db.create_tenant("t1", secret_key="key-t1")
    db.create_tenant("t2")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    db.register_device("d2", tenant_id="t2", paper_capacity=100)
    tenant_router.tenant_self_price_set(
        tenant_router.TenantPrice(bw=2.0), tenant=db.get_tenant_by_secret("key-t1"))
    assert db.effective_price("d1")["bw"] == 2.0
    assert db.effective_price("d1")["source"] == "tenant"
    assert db.effective_price("d2")["bw"] != 2.0


def test_self_price_route_declared_before_dynamic_route():
    """路由顺序回归：/api/tenant/self/price 必须排在 /api/tenant/{tenant_id}/price 之前。

    否则 FastAPI 会先用动态路由匹配（tenant_id="self"）到平台方定价接口，
    租户自助改价会被判 403 "merchant key required"（该问题曾真实出现）。
    """
    def index(path, method):
        for i, route in enumerate(tenant_router.router.routes):
            if route.path == path and method in (route.methods or set()):
                return i
        raise AssertionError("未注册路由：" + method + " " + path)

    assert index("/api/tenant/self/price", "POST") < index("/api/tenant/{tenant_id}/price", "POST")
    assert index("/api/tenant/self", "GET") < index("/api/tenant/{tenant_id}", "GET")


def test_expired_helper(temp_db):
    assert tenant_router._is_expired(None) is False
    assert tenant_router._is_expired("") is False
    assert tenant_router._is_expired("2000-01-01") is True
    assert tenant_router._is_expired("2999-01-01") is False
    assert tenant_router._mask_secret("short") == "****"
    masked = tenant_router._mask_secret("sk_1234567890abcdef")
    assert "****" in masked and masked.startswith("sk_123")


# ----------------------- SaaS：订阅生效 / 设备配额 / 营收结算 -----------------------

def test_expired_subscription_disables_service(temp_db):
    db.create_tenant("t1", secret_key="k-t1", expires_at="2000-01-01")
    state = db.tenant_service_state("t1")
    assert state["active"] is False
    assert "到期" in state["reason"]
    assert state["can_add_device"] is False
    # 未到期的租户正常
    db.create_tenant("t2", secret_key="k-t2", expires_at="2999-01-01")
    assert db.tenant_service_state("t2")["active"] is True


def test_device_register_enforces_printer_limit(temp_db):
    from routers import device as device_router

    db.create_tenant("t1", secret_key="k", printer_limit=1)
    first = device_router.device_register(
        device_router.DeviceRegister(device_id="d1", tenant_id="t1"))
    assert first.code == 200
    # 超过上限的新设备被 403 拦下
    second = device_router.device_register(
        device_router.DeviceRegister(device_id="d2", tenant_id="t1"))
    assert getattr(second, "status_code", None) == 403
    # 已存在设备重复注册不受配额影响
    again = device_router.device_register(
        device_router.DeviceRegister(device_id="d1", tenant_id="t1"))
    assert again.code == 200


def test_printer_available_scoped_to_tenant(temp_db):
    from routers import device as device_router

    db.create_tenant("t1", secret_key="k1")
    db.create_tenant("t2", secret_key="k2")
    db.register_device("d1", tenant_id="t1", paper_capacity=100)
    db.register_device("d2", tenant_id="t2", paper_capacity=100)
    db.update_device_config("d1", store_name="图书馆三楼",
                            address="图书馆 3F", business_hours="08:00-22:00",
                            speed_ppm=10)
    db.add_job({"folder": "q1", "filename": "a.pdf", "print_copies": 1,
                "print_range_start": 1, "print_range_end": 4,
                "print_side": "one-sided"}, printer_id="d1")

    own = device_router.printer_available(tenant_id="t1")
    first = own.data["printers"][0]
    assert [p["device_id"] for p in own.data["printers"]] == ["d1"]
    assert first["store_name"] == "图书馆三楼"
    assert first["business_hours"] == "08:00-22:00"
    assert first["queue_pages"] == 4
    assert first["wait_min"] == 0.4
    # 不传 tenant_id 时只看默认租户，不会看到别家的机器
    assert device_router.printer_available().data["printers"] == []

    db.set_tenant_status("t2", "suspended")
    stopped = device_router.printer_available(tenant_id="t2")
    assert stopped.data["printers"] == []
    assert stopped.data["service_active"] is False
    assert "停用" in stopped.data["message"]


def test_revenue_counts_paid_orders_only_and_settles_once(temp_db):
    db.create_tenant("t1", fee_rate=0.2)
    db.register_device("d1", tenant_id="t1", paper_capacity=100)

    def add(amount, trade_no=None, folder="f"):
        db.add_job({"folder": folder, "filename": "a.pdf", "print_copies": 1,
                    "print_range_start": 1, "print_range_end": 1,
                    "print_side": "one-sided", "amount": amount,
                    "out_trade_no": trade_no,
                    "eta_deadline": ("2000-01-01 00:00:00"
                                     if trade_no == "WX3" else None)},
                   printer_id="d1")

    add(10.0, "WX1", "w1")
    add(5.0, "WX2", "w2")
    add(99.0, None, "w3")      # 未付款，不计
    add(7.0, "WX3", "w4")      # 超时免单，不计
    db.apply_guarantee("w4", "a.pdf")

    summary = db.tenant_revenue_summary("t1")
    assert summary["total"] == {"orders": 2, "amount": 15.0}
    assert summary["fee_rate"] == 0.2
    assert summary["fee_total"] == 3.0
    assert summary["net_total"] == 12.0
    assert summary["unsettled"]["orders"] == 2
    assert summary["unsettled"]["net"] == 12.0

    record = db.settle_tenant("t1", note="首次结算")
    assert record["orders"] == 2 and record["amount"] == 15.0
    assert record["fee"] == 3.0 and record["net"] == 12.0

    after = db.tenant_revenue_summary("t1")
    assert after["unsettled"]["orders"] == 0
    assert after["total"] == {"orders": 2, "amount": 15.0}
    # 同一批订单不会被重复结算
    assert db.settle_tenant("t1")["orders"] == 0
    assert len(db.list_tenant_settlements("t1")) == 1


def test_expired_tenant_cannot_change_price(temp_db):
    db.create_tenant("t1", secret_key="k-t1", expires_at="2000-01-01")
    result = tenant_router.tenant_self_price_set(
        tenant_router.TenantPrice(bw=1.5),
        tenant=db.get_tenant_by_secret("k-t1"))
    assert result.message != "success"
    assert "订阅不可用" in result.message
