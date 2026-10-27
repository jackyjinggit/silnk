"""设备管理 API。

认证方式与 routers/printer.py 保持一致：请求头 Authorization: Bearer <SECRET_KEY>。
所有接口统一返回 ReturnResult。
"""

import hashlib
import math
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

import fitz  # PyMuPDF：生成打印测试页
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
from alerts import OFFLINE_TIMEOUT_SECONDS
from jwt import (auth_device_id, verify_admin_key, verify_device_or_merchant,
                 verify_merchant_key)
from models import ReturnResult
from print_queue import print_queue_singleton

router = APIRouter()


class DeviceRegister(BaseModel):
    """设备注册请求体。"""

    device_id: str
    name: str = ""
    model: str = ""
    driver: str = ""
    conn: str = ""
    paper_capacity: int = 0
    paper_state: Optional[str] = None
    # 设备端自动识别的能力（如 G3800 不支持双面）；管理端手动改过则以管理端为准
    supports_color: Optional[bool] = None
    supports_duplex: Optional[bool] = None
    # 本机已安装的打印机队列（客户端上报），用于管理端「新增设备」一键填入
    printers: Optional[list] = None
    # 设备归属的租户（客户端环境变量 TENANT_ID 上报）；留空时新设备归默认租户
    tenant_id: Optional[str] = None


class DeviceHeartbeat(BaseModel):
    """设备心跳请求体，传感器字段为可选。"""

    device_id: str
    status: str = "online"
    pages_printed: Optional[int] = None
    paper_left: Optional[int] = None
    ups_online: Optional[bool] = None
    door_open: Optional[bool] = None
    pickup_seq: Optional[int] = None
    paper_state: Optional[str] = None
    # 预留字段：UPS 剩余供电分钟数。设备暂未上报时保持为空，
    # R3 消息会如实标注「剩余时长待设备上报」，不编造数值。
    ups_runtime_min: Optional[int] = None
    printers: Optional[list] = None
    # 设备归属的租户（客户端环境变量 TENANT_ID 上报），心跳时用于修正归属
    tenant_id: Optional[str] = None


class DeviceClaimRequest(BaseModel):
    """工控机首次启动时提交的配对申请（无需鉴权，批准后可拿到设备令牌）。"""

    hostname: str = ""
    fingerprint: str = ""
    model: str = ""
    # 操作员在向导里选中的队列：批准时按它写驱动与能力，不能拿列表第一台凑数
    printer_name: str = ""
    printers: Optional[list] = None
    paper_capacity: int = 250


class ClaimApprove(BaseModel):
    """管理端批准配对：顺便把点位信息写进设备，客户端不用再配经纬度。"""

    tenant_id: str = "default"
    name: str = ""
    address: Optional[str] = None
    business_hours: Optional[str] = "07:00-23:00"
    lat: Optional[float] = None
    lng: Optional[float] = None
    paper_capacity: int = 250


_CLAIM_HITS = {}
_CLAIM_LOCK = threading.Lock()


def _claim_allowed(ip):
    """配对申请限速：同一 IP 每分钟最多 5 次，避免被刷爆待接入列表。"""
    now = time.time()
    with _CLAIM_LOCK:
        hits = [t for t in _CLAIM_HITS.get(ip, []) if now - t < 60]
        if len(hits) >= 5:
            _CLAIM_HITS[ip] = hits
            return False
        hits.append(now)
        _CLAIM_HITS[ip] = hits
        # 顺手清理长时间不活跃的 IP，避免字典无限增长
        if len(_CLAIM_HITS) > 500:
            for key in [k for k, v in _CLAIM_HITS.items()
                        if not v or now - v[-1] > 3600]:
                _CLAIM_HITS.pop(key, None)
        return True


@router.post("/api/device/claim")
def device_claim(request: DeviceClaimRequest, http_request: Request):
    """零配置接入第一步：设备申请配对，返回屏幕上显示的短码。

    接口故意不鉴权（新机器还没有任何凭据），但申请必须由管理端批准才会生效；
    待接入列表超过上限或同一 IP 刷得太快时拒绝。
    """
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not _claim_allowed(client_ip):
        return ReturnResult(200, "申请过于频繁，请一分钟后再试", {})
    if len(db.list_pending_device_claims()) >= 30:
        return ReturnResult(200, "待接入设备过多，请先在管理端处理", {})
    claim_id = "clm_" + secrets.token_hex(6)
    code = f"{secrets.randbelow(10000):04d}"
    secret = secrets.token_urlsafe(24)
    claim = db.create_device_claim(
        claim_id, code, secret,
        hostname=request.hostname,
        fingerprint=request.fingerprint,
        model=request.model,
        printers=request.printers,
        paper_capacity=request.paper_capacity,
        selected_printer=request.printer_name,
    )
    return ReturnResult(200, "success", {
        "claim_id": claim_id,
        "code": code,
        # secret 只返回给发起申请的客户端，用来轮询自己的配对结果
        "secret": secret,
        "expires_at": claim["expires_at"],
    })


@router.get("/api/device/claim/{claim_id}")
def device_claim_status(claim_id: str, secret: str = ""):
    """设备轮询配对结果：pending / bound / expired / rejected。

    必须带申请时拿到的 secret，否则别人猜到 claim_id 就能领走设备令牌。
    令牌明文只返回一次，取走后库里立即清空。
    """
    claim = db.get_device_claim(claim_id)
    if claim is None:
        return ReturnResult(200, "claim not found", {})
    expected = claim.get("secret_hash") or ""
    actual = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    if not secret or not secrets.compare_digest(expected, actual):
        return ReturnResult(200, "invalid secret", {})
    if claim["status"] == "pending" and claim["expires_at"] < int(datetime.now().timestamp()):
        db.expire_device_claims()
        claim["status"] = "expired"
    result = {"status": claim["status"], "device_id": claim.get("device_id")}
    if claim["status"] == "bound":
        token = db.take_claim_token(claim_id)
        if token:
            result["token"] = token
        else:
            # 已经取过一次：令牌不落前端，让用户重新配对最安全
            result["status"] = "token_taken"
    return ReturnResult(200, "success", result)


@router.get("/api/device/claims")
def device_claim_list(role: str = Depends(verify_admin_key)):
    """管理端：待接入设备列表（短码、主机名、发现的打印机）。"""
    claims = db.list_pending_device_claims()
    items = [{
        "claim_id": c["claim_id"],
        "code": c["code"],
        "hostname": c["hostname"],
        "model": c["model"],
        "selected_printer": c.get("selected_printer") or "",
        "printers": c["printers"],
        "paper_capacity": c["paper_capacity"],
        "created_at": c["created_at"],
        "expires_at": c["expires_at"],
    } for c in claims]
    return ReturnResult(200, "success", {"claims": items})


@router.post("/api/device/claims/{claim_id}/approve")
def device_claim_approve(claim_id: str, request: ClaimApprove,
                         role: str = Depends(verify_merchant_key)):
    """管理端批准接入：创建设备、写入点位信息、签发设备令牌。"""
    claim = db.get_device_claim(claim_id)
    if claim is None or claim["status"] != "pending":
        return ReturnResult(200, "该申请不存在或已处理", {})
    if claim["expires_at"] < int(datetime.now().timestamp()):
        db.expire_device_claims()
        return ReturnResult(200, "该申请已过期，请让设备重新申请", {})
    if request.lat is not None and not (-90 <= request.lat <= 90):
        return ReturnResult(200, "lat out of range", {})
    if request.lng is not None and not (-180 <= request.lng <= 180):
        return ReturnResult(200, "lng out of range", {})
    state = db.tenant_service_state(request.tenant_id or "default")
    if not state["active"]:
        return ReturnResult(200, f"该点位订阅不可用：{state['reason']}", {})
    if not state["can_add_device"]:
        return ReturnResult(
            200, f"已达打印机上限（{state['printer_limit']} 台）", {})

    device_id = "dev-" + secrets.token_hex(4)
    while db.get_device(device_id) is not None:
        device_id = "dev-" + secrets.token_hex(4)
    printers = claim.get("printers") or []
    selected = claim.get("selected_printer") or ""
    first = next((p for p in printers
                  if str(p.get("name") or "") == selected),
                 printers[0] if printers else {})
    db.register_device(
        device_id,
        request.name or claim.get("hostname") or "未命名点位",
        claim.get("model") or first.get("model", ""),
        first.get("driver", ""),
        first.get("port", ""),
        request.paper_capacity or claim.get("paper_capacity") or 250,
        printers=printers,
        supports_color=first.get("supports_color"),
        supports_duplex=first.get("supports_duplex"),
        tenant_id=request.tenant_id or "default",
    )
    db.update_device_config(
        device_id, address=request.address,
        business_hours=request.business_hours,
        lat=request.lat, lng=request.lng)
    token = secrets.token_urlsafe(32)
    approved = db.approve_device_claim(claim_id, device_id, token)
    if approved is None:
        # 并发场景下别人先批准了：把刚建的设备删掉，避免留孤儿设备
        db.delete_device(device_id)
        return ReturnResult(200, "该申请已被处理", {})
    return ReturnResult(200, "success", {
        "claim_id": claim_id,
        "device_id": device_id,
    })


@router.post("/api/device/claims/{claim_id}/reject")
def device_claim_reject(claim_id: str, role: str = Depends(verify_merchant_key)):
    """管理端拒绝接入：把申请标记为 rejected，设备端会看到提示。"""
    ok = db.reject_device_claim(claim_id)
    return ReturnResult(200, "success" if ok else "该申请不存在或已处理", {})


@router.post("/api/device/self/retire")
def device_self_retire(scope: str = Depends(verify_device_or_merchant)):
    """设备主动注销自己（向导里「重新配对」前调用），避免留下幽灵设备吃配额。

    只允许设备令牌调用；商家密钥请走 DELETE /api/device/{id}。
    """
    token_device = auth_device_id(scope)
    if not token_device:
        return ReturnResult(200, "只有设备令牌可以注销自己", {})
    deleted, _, _ = db.delete_device(token_device)
    return ReturnResult(200, "success" if deleted else "device not found", {})


@router.post("/api/device/register")
def device_register(request: DeviceRegister, scope: str = Depends(verify_device_or_merchant)):
    """新增或更新设备信息。

    SaaS 配额：新设备必须先通过租户订阅校验（租户可用 + 未超打印机上限）；
    已存在设备的重新注册不受配额影响，避免客户端重启后被挡在门外。
    零配置接入的设备令牌只能注册自己这台设备，且不能改租户归属。
    """
    token_device = auth_device_id(scope)
    if token_device and token_device != request.device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)
    tenant_id = None if token_device else request.tenant_id
    existing = db.get_device(request.device_id)
    if existing is None:
        state = db.tenant_service_state(tenant_id or "default")
        if not state["active"]:
            return JSONResponse(
                {"message": f"该点位订阅不可用：{state['reason']}"}, status_code=403)
        if not state["can_add_device"]:
            return JSONResponse(
                {"message": f"已达打印机上限（{state['printer_limit']} 台），"
                            f"请平台方升级套餐后再接入"}, status_code=403)
    device = db.register_device(
        request.device_id,
        request.name,
        request.model,
        request.driver,
        request.conn,
        request.paper_capacity,
        request.paper_state,
        printers=request.printers,
        supports_color=request.supports_color,
        supports_duplex=request.supports_duplex,
        tenant_id=tenant_id,
    )
    return ReturnResult(200, "success", {"device": device})


@router.post("/api/device/heartbeat")
def device_heartbeat(request: DeviceHeartbeat,
                     scope: str = Depends(verify_device_or_merchant)):
    """接收设备心跳，更新 last_heartbeat 及相关字段。"""
    token_device = auth_device_id(scope)
    if token_device and token_device != request.device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)
    device = db.update_heartbeat(
        request.device_id,
        status=request.status,
        pages_printed=request.pages_printed,
        paper_left=request.paper_left,
        ups_online=request.ups_online,
        door_open=request.door_open,
        pickup_seq=request.pickup_seq,
        paper_state=request.paper_state,
        ups_runtime_min=request.ups_runtime_min,
        printers=request.printers,
        tenant_id=None if token_device else request.tenant_id,
    )
    if device is None:
        return ReturnResult(200, "device not found", {})
    return ReturnResult(200, "success", {"device": device})


@router.get("/api/device/list")
def device_list(role: str = Depends(verify_admin_key)):
    """返回全部设备（含计算字段 queue_depth，按设备统计未完成订单）。"""
    devices = db.list_devices()
    open_jobs = db.load_open_jobs()
    for device in devices:
        device["queue_depth"] = sum(
            1 for job in open_jobs if job.get("printer_id") == device.get("device_id"))
    return ReturnResult(200, "success", {"devices": devices})


@router.get("/api/device/discovered")
def device_discovered(role: str = Depends(verify_admin_key)):
    """聚合各打印机端上报的本机队列，供管理端「新增设备」一键填入。

    必须放在 /api/device/{device_id} 之前，否则会被动态路由当成 device_id=discovered。
    """
    items, seen = [], set()
    for device in db.list_devices():
        online = _effective_status(device) == "online"
        for printer in device.get("printers") or []:
            name = str(printer.get("name") or "").strip()
            if not name:
                continue
            key = (device.get("device_id"), name)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                **printer,
                "device_id": device.get("device_id"),
                "device_name": device.get("name") or device.get("device_id"),
                "device_online": online,
                "last_heartbeat": device.get("last_heartbeat"),
            })
    items.sort(key=lambda x: (not x.get("in_use"), not x.get("device_online"),
                              x.get("name") or ""))
    return ReturnResult(200, "success", {"printers": items})


@router.get("/api/device/{device_id}")
def device_detail(device_id: str, role: str = Depends(verify_admin_key)):
    """返回单台设备详情（本机队列深度、paper_left、pages_printed 等）。"""
    device = db.get_device(device_id)
    if device is None:
        return ReturnResult(200, "device not found", {})
    device["queue_depth"] = sum(
        1 for job in db.load_open_jobs() if job.get("printer_id") == device_id)
    return ReturnResult(200, "success", {"device": device})


@router.delete("/api/device/{device_id}")
def device_delete(device_id: str, role: str = Depends(verify_merchant_key)):
    """删除设备及其告警/指令/日志；该设备未完成的订单标记为失败并从队列移除。"""
    deleted, removed_alerts, failed_jobs = db.delete_device(device_id)
    if not deleted:
        return ReturnResult(200, "device not found", {})
    queue = print_queue_singleton().data
    queue[:] = [job for job in queue if job.get("printer_id") != device_id]
    return ReturnResult(200, "success", {
        "device_id": device_id,
        "removed_alerts": removed_alerts,
        "failed_jobs": failed_jobs,
    })


class PaperConfig(BaseModel):
    """管理端设置纸盒容量 / 当前剩余张数的请求体。

    两个字段都可选：只给 paper_capacity 表示「设初始张数并视为刚补满」；
    给 paper_left 表示按实际剩余量校准（估算从该值继续递减）。
    """

    paper_capacity: Optional[int] = None
    paper_left: Optional[int] = None


class DeviceConfig(BaseModel):
    """设备展示名、门店地址、营业时间、经纬度与能力参数（都可选）。"""

    name: Optional[str] = None
    store_name: Optional[str] = None
    address: Optional[str] = None
    business_hours: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    supports_color: Optional[bool] = None
    supports_duplex: Optional[bool] = None
    speed_ppm: Optional[float] = None


@router.post("/api/device/{device_id}/config")
def device_set_config(device_id: str, request: DeviceConfig,
                      role: str = Depends(verify_admin_key)):
    """更新设备名称、门店位置与能力参数（用于智能选机）。"""
    if request.lat is not None and not (-90 <= request.lat <= 90):
        return ReturnResult(200, "lat out of range", {})
    if request.lng is not None and not (-180 <= request.lng <= 180):
        return ReturnResult(200, "lng out of range", {})
    if request.speed_ppm is not None and request.speed_ppm <= 0:
        return ReturnResult(200, "speed_ppm must be > 0", {})
    device = db.update_device_config(
        device_id,
        name=request.name,
        store_name=request.store_name,
        address=request.address,
        business_hours=request.business_hours,
        lat=request.lat,
        lng=request.lng,
        supports_color=request.supports_color,
        supports_duplex=request.supports_duplex,
        speed_ppm=request.speed_ppm,
    )
    if device is None:
        return ReturnResult(200, "device not found", {})
    return ReturnResult(200, "success", {"device": device})


@router.post("/api/device/{device_id}/paper")
def device_set_paper(device_id: str, request: PaperConfig,
                     role: str = Depends(verify_admin_key)):
    """设置设备的纸盒容量与当前剩余张数（设置剩余量即一次校准）。"""
    if request.paper_capacity is None and request.paper_left is None:
        return ReturnResult(200, "nothing to update", {})
    if request.paper_capacity is not None and request.paper_capacity < 0:
        return ReturnResult(200, "paper_capacity must be >= 0", {})
    if request.paper_left is not None and request.paper_left < 0:
        return ReturnResult(200, "paper_left must be >= 0", {})
    device = db.set_paper_config(device_id, request.paper_capacity, request.paper_left)
    if device is None:
        return ReturnResult(200, "device not found", {})
    return ReturnResult(200, "success", {"device": device})


# ----------------------- 管理端下发指令 / 查看日志 -----------------------

SELFTEST_DIR = "pending_files/_selftest"
ALLOWED_COMMANDS = ("restart", "test_page")


class DeviceCommand(BaseModel):
    """管理端指令请求体：restart（重启打印机端）/ test_page（打印测试页）。"""

    command: str


def _draw_test_page(path, device_id):
    """生成一页 A4 测试页：文字 + 色块 + 细线，验证打印链路和色彩。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4，单位 pt
    page.insert_text((60, 90), "SILNK TEST PAGE", fontsize=26, fontname="helv")
    page.insert_text((60, 126), "device: " + device_id, fontsize=13, fontname="helv")
    page.insert_text((60, 148), "time: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     fontsize=13, fontname="helv")
    try:
        # PyMuPDF 内置简体中文字体；万一环境不支持就退回英文，不让测试页生成失败
        page.insert_text((60, 188), "思印客 · 打印测试页", fontsize=20, fontname="china-s")
    except Exception:
        page.insert_text((60, 188), "Silnk Print Test Page", fontsize=18, fontname="helv")
    colors = [(0, 0, 0), (0, 1, 1), (1, 0, 1), (1, 1, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    for index, color in enumerate(colors):
        left = 60 + index * 68
        page.draw_rect(fitz.Rect(left, 220, left + 60, 300), color=color, fill=color)
    for line in range(12):
        y = 340 + line * 12
        page.draw_line(fitz.Point(60, y), fitz.Point(520, y), color=(0, 0, 0), width=0.5)
    page.insert_text((60, 520),
                     "If this page prints correctly, the print path works.",
                     fontsize=11, fontname="helv")
    page.insert_text((60, 540),
                     "If colors look right, the color print path works.",
                     fontsize=11, fontname="helv")
    doc.save(path)
    doc.close()


@router.post("/api/device/{device_id}/command")
def device_command(device_id: str, request: DeviceCommand,
                   role: str = Depends(verify_admin_key)):
    """管理端下发指令。

    - restart：写入 device_commands，客户端轮询取走后重启自己（10 分钟未取走自动过期）
    - test_page：后端生成一页测试 PDF 并直接入队，走与正常订单完全相同的打印链路
    """
    device = db.get_device(device_id)
    if device is None:
        return ReturnResult(200, "device not found", {})
    command = (request.command or "").strip()
    if command not in ALLOWED_COMMANDS:
        return ReturnResult(200, "unknown command", {})
    online = _effective_status(device) == "online"

    if command == "test_page":
        os.makedirs(SELFTEST_DIR, exist_ok=True)
        # 文件名必须唯一：两台设备同一秒内打印测试页时，
        # 只用秒级时间戳会重名，导致后一份覆盖前一份、订单互相串
        filename = ("testpage-" + datetime.now().strftime("%Y%m%d%H%M%S")
                    + "-" + uuid.uuid4().hex[:6] + ".pdf")
        _draw_test_page(os.path.join(SELFTEST_DIR, filename), device_id)
        job = {
            "folder": "_selftest",
            "filename": filename,
            "print_copies": 1,
            "print_range_start": 1,
            "print_range_end": 1,
            "print_side": "one-sided",
            "paper": "A4",
            "print_color": "color",
            "amount": 0,
            "status": "pending",
            "printer_id": device_id,
        }
        print_queue_singleton().data.append(job)
        job_id = db.add_job(job, printer_id=device_id)
        return ReturnResult(200, "success", {
            "command": command,
            "job_id": job_id,
            "file": filename,
            "online": online,
            "note": ("测试页已加入打印队列，设备在线时约 1 秒内开始打印"
                     if online else "测试页已入队，但设备当前离线，恢复上线后会打印"),
        })

    command_id = db.add_command(device_id, "restart")
    return ReturnResult(200, "success", {
        "command": command,
        "command_id": command_id,
        "online": online,
        "note": ("重启指令已下发，打印机端会在 1 秒内执行"
                 if online else "设备当前离线，指令已排队；10 分钟内上线会执行，超时自动过期"),
    })


@router.get("/api/device/{device_id}/commands")
def device_command_list(device_id: str, role: str = Depends(verify_admin_key)):
    """查看某台设备最近的指令及执行结果。"""
    return ReturnResult(200, "success", {"commands": db.list_commands(device_id)})


@router.get("/api/device/{device_id}/logs")
def device_logs(device_id: str, limit: int = 200, role: str = Depends(verify_admin_key)):
    """读取打印机端上报的实时日志。

    如实说明：日志来自 client 进程（Windows 下是 SumatraPDF 链路，Linux 下是 CUPS/ipptool），
    由客户端每次心跳后推送；服务器本身没有运行 CUPS，因此这里不是 CUPS 服务日志。
    """
    lines, updated_at = db.get_device_logs(device_id, limit)
    return ReturnResult(200, "success", {
        "device_id": device_id,
        "lines": lines,
        "updated_at": updated_at,
        "source": "打印机端 client 上报的日志（不是服务器 CUPS 服务日志）",
    })


def _effective_status(device):
    """按心跳时间现算在线状态，返回 "online" / "offline"。

    数据库里的 status 字段只有心跳会写 online，没有任何地方会写 offline，
    因此不能直接拿来给用户端展示——客户端进程被杀掉后会一直显示在线。
    这里与告警引擎 R2 用同一个阈值：超过 OFFLINE_TIMEOUT_SECONDS 秒无心跳即离线，
    从未上报过心跳的设备同样视为离线。
    """
    last = device.get("last_heartbeat")
    if not last:
        return "offline"
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "offline"
    if (datetime.now() - last_dt).total_seconds() > OFFLINE_TIMEOUT_SECONDS:
        return "offline"
    return "online"


def _queue_pages_by_device():
    """统计每台设备与「未指定设备」的待打印页数，用于排队与预计等待展示。"""
    device_pages = {}
    shared_pages = 0
    for job in db.load_open_jobs():
        try:
            start = int(job.get("print_range_start") or 1)
            end = int(job.get("print_range_end") or start)
            copies = max(int(job.get("print_copies") or 1), 1)
        except (TypeError, ValueError):
            continue
        pages = max(end - start + 1, 0) * copies
        owner = job.get("printer_id")
        if owner:
            device_pages[owner] = device_pages.get(owner, 0) + pages
        else:
            shared_pages += pages
    return device_pages, shared_pages


@router.get("/api/printer/available")
def printer_available(tenant_id: str = ""):
    """用户端可选打印机列表（只读、精简、无需 SECRET_KEY）。

    多租户入口：用户从某个点位的链接（/?t=<tenant_id>）进入时只看到该租户的设备；
    不传 tenant_id 时按默认租户处理，原有单租户部署行为不变。
    订阅停用 / 到期时返回空列表并给出原因，不展示不可下单的机器。
    status 按 last_heartbeat 现算（超 60 秒无心跳即离线），不直接信数据库里的 status。
    UPS 字段为 None 时表示设备尚未上报，前端应如实显示「未接入」，不得编造数值。
    """
    state = db.tenant_service_state(tenant_id or "default")
    devices = db.list_devices(state["tenant_id"]) if state["active"] else []
    device_pages, shared_pages = _queue_pages_by_device()
    printers = []
    for device in devices:
        device_id = device.get("device_id")
        queue_pages = device_pages.get(device_id, 0) + shared_pages
        try:
            speed = float(device.get("speed_ppm") or 10.0)
        except (TypeError, ValueError):
            speed = 10.0
        if speed <= 0:
            speed = 10.0
        printers.append({
            "device_id": device_id,
            "name": device.get("name"),
            "status": _effective_status(device),
            # 点位信息：用户到店前最关心的地址、营业时间与排队情况
            "store_name": device.get("store_name"),
            "address": device.get("address"),
            "business_hours": device.get("business_hours"),
            "lat": device.get("lat"),
            "lng": device.get("lng"),
            "queue_pages": queue_pages,
            "wait_min": round(queue_pages / speed, 1),
            # UPS 供电状态：True=市电正常、False=市电断开（UPS 供电中）、None=设备未上报
            "ups_online": device.get("ups_online"),
            # UPS 剩余供电分钟数：设备未上报时为 None（前端不显示具体分钟数）
            "ups_runtime_min": device.get("ups_runtime_min"),
        })
    return ReturnResult(200, "success", {
        "printers": printers,
        "tenant": state,
        "service_active": state["active"],
        "message": "" if state["active"] else f"该点位服务暂停：{state['reason']}",
    })


class RecommendRequest(BaseModel):
    """智能选机请求：本次订单的页数/张数、色彩/双面需求与用户位置。"""

    pages: int = 0
    sheets: int = 0
    color: bool = False
    duplex: bool = False
    paper: str = "A4"
    lat: Optional[float] = None
    lng: Optional[float] = None


def _distance_m(lat1, lng1, lat2, lng2):
    """Haversine 直线距离（米）。"""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


@router.post("/api/printer/recommend")
def printer_recommend(request: RecommendRequest, tenant_id: str = ""):
    """按在线状态、能力、纸量、队列和距离推荐打印机（无需密钥）。

    评分越低越优：
      - 预计等待 =（该机排队页数 + 本次页数）/ 该机速度
      - 距离按 5 分钟/公里折算
      - 不支持彩色 +100、不支持自动双面 +20、纸量可能不足 +30
    没有用户经纬度时不做距离评分，reasons 里会如实说明。
    与 /api/printer/available 一致：只在指定租户（默认租户）内推荐。
    """
    jobs = db.load_open_jobs()
    shared_pages = 0
    device_pages = {}
    for job in jobs:
        try:
            start = int(job.get("print_range_start") or 1)
            end = int(job.get("print_range_end") or start)
            copies = max(int(job.get("print_copies") or 1), 1)
        except (TypeError, ValueError):
            continue
        pages = max(end - start + 1, 0) * copies
        owner = job.get("printer_id")
        if owner:
            device_pages[owner] = device_pages.get(owner, 0) + pages
        else:
            shared_pages += pages

    candidates = []
    state = db.tenant_service_state(tenant_id or "default")
    for device in (db.list_devices(state["tenant_id"]) if state["active"] else []):
        device_id = device.get("device_id")
        if not device_id:
            continue
        if _effective_status(device) != "online":
            continue

        queue_pages = device_pages.get(device_id, 0) + shared_pages
        try:
            speed = float(device.get("speed_ppm") or 10.0)
        except (TypeError, ValueError):
            speed = 10.0
        if speed <= 0:
            speed = 10.0
        wait_min = (queue_pages + max(request.pages, 0)) / speed

        paper_left = device.get("paper_left")
        paper_cap = device.get("paper_capacity")
        try:
            paper_ok = paper_left is None or int(request.sheets) <= int(paper_left)
        except (TypeError, ValueError):
            paper_ok = True

        supports_color = device.get("supports_color")
        color_ok = (not request.color) or supports_color is None or bool(supports_color)
        supports_duplex = device.get("supports_duplex")
        duplex_ok = (not request.duplex) or supports_duplex is None or bool(supports_duplex)

        distance_m = None
        if (request.lat is not None and request.lng is not None
                and device.get("lat") is not None and device.get("lng") is not None):
            distance_m = _distance_m(
                request.lat, request.lng,
                float(device["lat"]), float(device["lng"]))

        score = wait_min + (distance_m / 1000.0 * 5.0 if distance_m is not None else 0.0)
        penalties = []
        if not color_ok:
            score += 100
            penalties.append("不支持彩色")
        if not duplex_ok:
            score += 20
            penalties.append("不支持自动双面")
        if not paper_ok:
            score += 30
            penalties.append("纸量可能不足")

        reasons = []
        if request.color and supports_color:
            reasons.append("支持彩色")
        if request.duplex and supports_duplex:
            reasons.append("支持自动双面")
        if paper_left is not None:
            reasons.append(f"纸量 {paper_left}/{paper_cap or 0}")
        else:
            reasons.append("纸量未上报")
        reasons.append(f"排队 {queue_pages} 页，预计 {wait_min:.1f} 分钟")
        if distance_m is not None:
            reasons.append(f"距离 {distance_m / 1000:.1f} km")
        else:
            reasons.append("未授权定位，未计入距离")

        candidates.append({
            "device_id": device_id,
            "name": device.get("name"),
            "store_name": device.get("store_name"),
            "score": round(score, 2),
            "wait_min": round(wait_min, 1),
            "queue_pages": queue_pages,
            "paper_left": paper_left,
            "paper_capacity": paper_cap,
            "supports_color": supports_color,
            "supports_duplex": supports_duplex,
            "distance_m": None if distance_m is None else round(distance_m, 1),
            "reasons": reasons,
            "penalties": penalties,
        })

    candidates.sort(key=lambda item: item["score"])
    best = candidates[0] if candidates else None
    return ReturnResult(200, "success", {
        "recommended": best["device_id"] if best else None,
        "distance_used": any(item["distance_m"] is not None for item in candidates),
        "candidates": candidates,
    })
