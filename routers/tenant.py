"""租户（软件订阅线）API。

最小多租户骨架：一个租户 = 一个客户（打印点 / 学校文印室）。
- 平台方用全局 SECRET_KEY（商家密钥）开通 / 维护租户：
  POST /api/tenant/create 等。
- 租户用自己租户的密钥访问 /api/tenant/self/*，只能看 / 改自己租户的数据。

本阶段为「手动开通、手动续期、手动结算」：订阅到期会真正停业务（不能再下单），
平台统一收款后按租户记账抽成并线下打款；在线支付与自动分账仍为待做（见 /api/status）。
所有接口统一返回 ReturnResult。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import db
from jwt import verify_merchant_key, verify_tenant_key
from models import ReturnResult

router = APIRouter()


def _mask_secret(value):
    """密钥脱敏展示：保留前 6 位与后 4 位，中间打码。"""
    text = str(value or "")
    if len(text) <= 12:
        return "****"
    return text[:6] + "****" + text[-4:]


def _public_tenant(tenant, reveal_secret=False):
    """租户对外展示结构：密钥默认脱敏，仅在开通返回体里明文给出便于交付。

    reveal_secret 只用于 POST /api/tenant/create 的响应——密钥只在此刻明文可见一次，
    列表/详情一律脱敏，避免管理端截图泄露租户密钥。
    """
    if not tenant:
        return None
    data = dict(tenant)
    secret = data.get("secret_key")
    data["secret_key"] = secret if reveal_secret else _mask_secret(secret)
    return data


# ----------------------- 平台方：开通 / 维护租户 -----------------------

class TenantCreate(BaseModel):
    """开通租户请求体。

    secret_key 留空时后端自动生成一个随机密钥（返回体里明文给出一次）。
    expires_at 为订阅到期时间（unix 秒或 ISO 字符串，可留空表示不限期）。
    """

    tenant_id: str
    name: str = ""
    secret_key: Optional[str] = None
    plan: str = "starter"
    printer_limit: int = 1
    expires_at: Optional[str] = None
    contact: str = ""
    note: str = ""
    # 平台抽成比例（0–1）；平台统一收款后按此比例记账，实际打款线下完成
    fee_rate: float = 0.1


@router.post("/api/tenant/create")
def tenant_create(request: TenantCreate, role: str = Depends(verify_merchant_key)):
    """开通一个租户（同时写入初始订阅）。tenant_id 已存在时返回明确提示。"""
    tenant_id = (request.tenant_id or "").strip()
    if not tenant_id:
        return ReturnResult(200, "tenant_id 不能为空", {})
    tenant = db.create_tenant(
        tenant_id,
        name=request.name,
        secret_key=request.secret_key,
        plan=request.plan,
        printer_limit=request.printer_limit,
        expires_at=request.expires_at,
        contact=request.contact,
        note=request.note,
        fee_rate=request.fee_rate,
    )
    if tenant is None:
        return ReturnResult(200, "租户已存在，请换一个 tenant_id", {})
    # 开通响应里明文给出密钥，便于一次性交付给客户；此后列表/详情只给脱敏值
    return ReturnResult(200, "success", {"tenant": _public_tenant(tenant, reveal_secret=True)})


@router.get("/api/tenant/list")
def tenant_list(role: str = Depends(verify_merchant_key)):
    """全部租户（含订阅），密钥脱敏展示。"""
    tenants = db.list_tenants()
    return ReturnResult(200, "success", {
        "tenants": [_public_tenant(t) for t in tenants],
    })


class TenantStatus(BaseModel):
    """启用 / 停用租户：active / suspended。"""

    status: str


@router.post("/api/tenant/{tenant_id}/status")
def tenant_status(tenant_id: str, request: TenantStatus,
                  role: str = Depends(verify_merchant_key)):
    """停用或启用租户；停用后该租户密钥立即失效。"""
    tenant = db.set_tenant_status(tenant_id, (request.status or "").strip())
    if tenant is None:
        return ReturnResult(200, "租户不存在或状态非法", {})
    return ReturnResult(200, "success", {"tenant": _public_tenant(tenant)})


class SubscriptionUpdate(BaseModel):
    """修改订阅：套餐名 / 打印机上限 / 到期时间 / 状态，均可选。

    printer_limit=-1 表示不限量（内部按空值处理，不强制上限）。
    """

    plan: Optional[str] = None
    printer_limit: Optional[int] = None
    expires_at: Optional[str] = None
    status: Optional[str] = None
    note: Optional[str] = None
    # 平台抽成比例（0–1），只影响之后的新结算
    fee_rate: Optional[float] = None


@router.post("/api/tenant/{tenant_id}/subscription")
def tenant_subscription(tenant_id: str, request: SubscriptionUpdate,
                        role: str = Depends(verify_merchant_key)):
    """续期 / 改套餐 / 改订阅状态（手动维护）。"""
    limit = request.printer_limit
    if limit is not None and limit < 0:
        limit = None
    subscription = db.set_subscription(
        tenant_id,
        plan=request.plan,
        printer_limit=limit,
        expires_at=request.expires_at,
        status=request.status,
        note=request.note,
        fee_rate=request.fee_rate,
    )
    if subscription is None:
        return ReturnResult(200, "租户不存在", {})
    return ReturnResult(200, "success", {"subscription": subscription})


class TenantPrice(BaseModel):
    """平台方设置某租户的默认单价（元/页、元/张）。"""

    bw: Optional[float] = None
    color: Optional[float] = None
    bw_duplex: Optional[float] = None
    color_duplex: Optional[float] = None


@router.delete("/api/tenant/{tenant_id}/price")
def tenant_price_clear(tenant_id: str, role: str = Depends(verify_merchant_key)):
    """清除某租户默认价，使其恢复跟随平台全局价。"""
    if db.get_tenant(tenant_id) is None:
        return ReturnResult(200, "租户不存在", {})
    removed = db.clear_tenant_price(tenant_id)
    return ReturnResult(200, "success", {"removed": removed})


# ----------------------- 租户自助：只看自己租户 -----------------------
# 注意：/api/tenant/self/* 必须声明在 /api/tenant/{tenant_id} 之前，
# 否则 "self" 会被动态路由当成 tenant_id 匹配掉。

@router.get("/api/tenant/self")
def tenant_self(tenant: dict = Depends(verify_tenant_key)):
    """当前租户信息：租户 + 订阅 + 生效默认价 + 设备/订单计数（不含密钥明文）。"""
    tenant_id = tenant["tenant_id"]
    devices = db.list_devices(tenant_id)
    jobs = db.list_jobs(tenant_id)
    subscription = tenant.get("subscription") or {}
    expires_at = subscription.get("expires_at")
    service = db.tenant_service_state(tenant_id)
    return ReturnResult(200, "success", {
        "tenant": _public_tenant(tenant),
        "devices_total": len(devices),
        "jobs_total": len(jobs),
        "expired": _is_expired(expires_at),
        "printer_limit": subscription.get("printer_limit"),
        "plan": subscription.get("plan"),
        # 订阅是否真正生效：停止 / 到期后不能下单，也不能再改价
        "service": service,
        "revenue": db.tenant_revenue_summary(tenant_id),
        # 本租户默认价（未设置过为 None，表示跟随平台全局价）
        "price": db.get_tenant_price(tenant_id),
        # 平台全局价：租户页展示「当前跟随的价」用，便于判断自己是否单独定过价
        "global_price": db.get_price_config(),
    })


def _is_expired(expires_at):
    """订阅是否已到期；expires_at 支持 unix 秒或可解析的时间字符串，空值表示不限期。"""
    if not expires_at:
        return False
    try:
        value = float(expires_at)
        if value > 1e12:  # 毫秒时间戳按秒处理
            value = value / 1000.0
        return datetime.now().timestamp() > value
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.now() > datetime.strptime(str(expires_at), fmt)
        except ValueError:
            continue
    return False


@router.get("/api/tenant/self/devices")
def tenant_self_devices(tenant: dict = Depends(verify_tenant_key)):
    """当前租户自己的设备列表。"""
    devices = db.list_devices(tenant["tenant_id"])
    for device in devices:
        # 租户不需要看到其他租户的敏感字段；保留运行必需字段即可
        device.pop("secret_key", None)
    return ReturnResult(200, "success", {"devices": devices})


@router.get("/api/tenant/self/orders")
def tenant_self_orders(tenant: dict = Depends(verify_tenant_key)):
    """当前租户自己的订单流水（含状态与本人订单金额）。"""
    orders = db.list_jobs(tenant["tenant_id"])
    for order in orders:
        order["filename"] = order.get("display_name") or order.get("filename")
    return ReturnResult(200, "success", {"orders": orders})


@router.get("/api/tenant/self/alerts")
def tenant_self_alerts(tenant: dict = Depends(verify_tenant_key)):
    """当前租户自己设备的告警。"""
    alerts = db.list_alerts(tenant_id=tenant["tenant_id"])
    return ReturnResult(200, "success", {"alerts": alerts})


@router.get("/api/tenant/self/revenue")
def tenant_self_revenue(tenant: dict = Depends(verify_tenant_key)):
    """本租户营收：今日 / 本月 / 累计、平台抽成、应结金额与结算记录。"""
    return ReturnResult(200, "success",
                        db.tenant_revenue_summary(tenant["tenant_id"]))


@router.get("/api/tenant/self/settlements")
def tenant_self_settlements(tenant: dict = Depends(verify_tenant_key)):
    """本租户历史结算批次（只读，结算由平台方发起）。"""
    return ReturnResult(200, "success", {
        "settlements": db.list_tenant_settlements(tenant["tenant_id"]),
    })


@router.post("/api/tenant/self/price")
def tenant_self_price_set(request: TenantPrice,
                          tenant: dict = Depends(verify_tenant_key)):
    """租户改自己租户的默认单价（只影响本租户设备，不影响其他租户）。"""
    service = db.tenant_service_state(tenant["tenant_id"])
    if not service["active"]:
        return ReturnResult(200, f"订阅不可用（{service['reason']}），暂不能改价", {})
    for name, value in (("bw", request.bw), ("color", request.color),
                        ("bw_duplex", request.bw_duplex),
                        ("color_duplex", request.color_duplex)):
        if value is not None and value <= 0:
            return ReturnResult(200, f"{name} must be > 0", {})
    price = db.set_tenant_price(tenant["tenant_id"], request.bw, request.color,
                                request.bw_duplex, request.color_duplex)
    return ReturnResult(200, "success", {"price": price})


# 平台方定价 POST 必须声明在 /api/tenant/self/price 之后：
# 否则 "/api/tenant/self/price" 会被 {tenant_id} 动态路由先匹配掉
# （tenant_id="self"），租户自助改价会直接 403。
@router.post("/api/tenant/{tenant_id}/price")
def tenant_price_set(tenant_id: str, request: TenantPrice,
                     role: str = Depends(verify_merchant_key)):
    """平台方设置某租户的默认单价；未设置的项跟随平台全局价。"""
    if db.get_tenant(tenant_id) is None:
        return ReturnResult(200, "租户不存在", {})
    for name, value in (("bw", request.bw), ("color", request.color),
                        ("bw_duplex", request.bw_duplex),
                        ("color_duplex", request.color_duplex)):
        if value is not None and value <= 0:
            return ReturnResult(200, f"{name} must be > 0", {})
    price = db.set_tenant_price(tenant_id, request.bw, request.color,
                                request.bw_duplex, request.color_duplex)
    return ReturnResult(200, "success", {"price": price})


class SettleBody(BaseModel):
    """平台方结算：备注可选；结算范围是「所有已付款未结算订单」。"""

    note: str = ""


@router.get("/api/tenant/{tenant_id}/revenue")
def tenant_revenue_detail(tenant_id: str,
                          role: str = Depends(verify_merchant_key)):
    """平台方查看某租户营收、抽成与未结算金额。"""
    if db.get_tenant(tenant_id) is None:
        return ReturnResult(200, "租户不存在", {})
    return ReturnResult(200, "success", db.tenant_revenue_summary(tenant_id))


@router.post("/api/tenant/{tenant_id}/settle")
def tenant_settle(tenant_id: str, body: SettleBody,
                  role: str = Depends(verify_merchant_key)):
    """把该租户已付款未结算订单打成一批，记录抽成与应结金额。

    只做账目结算，不触发真实打款；打款仍由平台方线下完成。
    """
    record = db.settle_tenant(tenant_id, operator="平台方", note=body.note)
    if record is None:
        return ReturnResult(200, "租户不存在", {})
    if not record.get("orders"):
        return ReturnResult(200, "没有待结算订单", record)
    return ReturnResult(200, "success", record)


@router.get("/api/tenant/{tenant_id}/settlements")
def tenant_settlement_list(tenant_id: str,
                           role: str = Depends(verify_merchant_key)):
    if db.get_tenant(tenant_id) is None:
        return ReturnResult(200, "租户不存在", {})
    return ReturnResult(200, "success", {
        "settlements": db.list_tenant_settlements(tenant_id),
    })


@router.get("/api/tenant/{tenant_id}")
def tenant_detail(tenant_id: str, role: str = Depends(verify_merchant_key)):
    """租户详情：租户 + 订阅 + 该租户默认价 + 设备/订单计数。

    必须声明在 /api/tenant/self/* 之后：否则 "/api/tenant/self" 会被
    {tenant_id} 动态路由先匹配掉，self 被当成租户标识。
    """
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return ReturnResult(200, "租户不存在", {})
    devices = db.list_devices(tenant_id)
    jobs = db.list_jobs(tenant_id)
    return ReturnResult(200, "success", {
        "tenant": _public_tenant(tenant),
        "price": db.get_tenant_price(tenant_id),
        "devices_total": len(devices),
        "jobs_total": len(jobs),
        "service": db.tenant_service_state(tenant_id),
        "revenue": db.tenant_revenue_summary(tenant_id),
    })
