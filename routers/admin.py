"""管理端 API：订单流水、单机定价、收款流水与接入状态。"""

import csv
import io
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel

import db
import delivery
import notify
import pricing
from jwt import verify_admin_key, verify_merchant_key
from models import ReturnResult

router = APIRouter()

# 收款码图片目录（main.py 以 /pay-qr 挂载）。
# 用于如实判断收款码是否真的存在，不假装已配置。
PAY_QR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static", "pay")


def _qr_file_exists(url):
    """按收款码 URL 反查本地文件是否存在（如 /pay-qr/wechat_qr.png → static/pay/wechat_qr.png）。"""
    if not url:
        return False
    name = url.rstrip("/").split("/")[-1]
    return os.path.isfile(os.path.join(PAY_QR_DIR, name))


class PriceSettings(BaseModel):
    """管理端修改单价的请求体（元/页）；只传其中一项时另一项保持原值。"""

    bw: Optional[float] = None
    color: Optional[float] = None
    bw_duplex: Optional[float] = None
    color_duplex: Optional[float] = None


class PaySettings(BaseModel):
    """管理端修改收款配置（收款码路径 / 收款人昵称）。"""

    wechat_qr: Optional[str] = None
    payee: Optional[str] = None


@router.get("/api/whoami")
def whoami(role: str = Depends(verify_admin_key)):
    """管理端登录后确认当前密钥的角色：merchant / ops。"""
    return ReturnResult(200, "success", {"role": role})


# ----------------------- 用户账号管理（商家专属） -----------------------

class BanRequest(BaseModel):
    """封禁原因（会展示给被封禁用户）。"""

    reason: str = "违反平台规定"


def _mask_phone(phone):
    """手机号脱敏：138****8000。管理端不再直接展示完整号码。"""
    value = str(phone or "")
    return value[:3] + "****" + value[-4:] if len(value) == 11 else value


@router.get("/api/user/list")
def user_list(role: str = Depends(verify_merchant_key)):
    """注册用户列表（手机号脱敏，避免管理端随手截图泄露完整号码）。"""
    users = db.list_users()
    for user in users:
        user["phone"] = _mask_phone(user.get("phone"))
    return ReturnResult(200, "success", {"users": users})


@router.post("/api/user/{user_id}/ban")
def user_ban(user_id: int, request: BanRequest,
             role: str = Depends(verify_merchant_key)):
    """封禁账号：立即生效，已登录的旧 token 下一次请求即 403。"""
    if db.get_user(user_id) is None:
        return ReturnResult(200, "用户不存在", {})
    reason = (request.reason or "").strip() or "违反平台规定"
    user = db.set_user_status(user_id, "banned", reason)
    return ReturnResult(200, "success", {"user": user})


@router.post("/api/user/{user_id}/unban")
def user_unban(user_id: int, role: str = Depends(verify_merchant_key)):
    """解封账号，恢复登录与下单。"""
    if db.get_user(user_id) is None:
        return ReturnResult(200, "用户不存在", {})
    user = db.set_user_status(user_id, "active", None)
    return ReturnResult(200, "success", {"user": user})


@router.get("/api/order/list")
def order_list(role: str = Depends(verify_admin_key)):
    """订单/取件流水（基于 print_jobs，含状态）。

    运维可以看订单状态用于排障，但不返回金额/省下金额。
    """
    orders = db.list_jobs()
    for order in orders:
        # 打印机端文件名是订单号；管理端展示真实文件名
        order["filename"] = order.get("display_name") or order.get("filename")
        if order.get("batch_pages") and order.get("batch_copies"):
            # 批量包的总页数包含封皮与全部份数，管理端按「每份页数 × 份数」展示
            order["print_range_start"] = 1
            order["print_range_end"] = order["batch_pages"]
            order["print_copies"] = order["batch_copies"]
            order["batch_bundle"] = True
        if role == "ops":
            # 运维只看运行状态，不看金额
            order.pop("amount", None)
            order.pop("saved_amount", None)
    return ReturnResult(200, "success", {"orders": orders})


class ReprintReview(BaseModel):
    """重印审核备注（选填）。"""

    note: str = ""


@router.get("/api/order/reprint/list")
def reprint_list(role: str = Depends(verify_admin_key)):
    """重印申请列表：用户端提交的「格式不对免费重印」申请及处理状态。"""
    requests = db.list_reprint_requests()
    for item in requests:
        # 打印机端文件名是订单号；展示真实文件名便于核对
        item["filename"] = item.get("display_name") or item.get("filename")
    return ReturnResult(200, "success", {"requests": requests})


@router.post("/api/order/{order_id}/reprint/approve")
def reprint_approve(order_id: int, request: ReprintReview,
                    role: str = Depends(verify_merchant_key)):
    """通过重印申请：免费重新入队打印（金额 0，不计费、不改价）。

    源文件若已按承诺「打印完成 5 分钟后」删除，则返回明确提示，
    让用户重新上传（重印仍免费），绝不假装已重印。
    """
    job = db.get_job_by_id(order_id)
    if job is None:
        return ReturnResult(200, "订单不存在", {})
    if job.get("reprint_status") != "requested":
        return ReturnResult(200, "该订单没有待处理的重印申请", {})
    folder = job.get("folder")
    display = job.get("display_name") or job.get("filename") or ""
    pdf_name = display.rsplit(".", 1)[0] + ".pdf"
    # 优先用付款时另存的重印副本（订单完成 5 分钟后清理），
    # 副本不在时才回落到会话目录（刚下单还没清理的情况）。
    source_path = None
    for candidate in (f"reprint_cache/{folder}/{pdf_name}",
                      f"uploads/{folder}/converted/{pdf_name}"):
        if os.path.isfile(candidate):
            source_path = candidate
            break
    if source_path is None:
        return ReturnResult(200, "源文件已按承诺删除，请让用户重新上传，重印仍免费", {})
    # 延迟导入 pay，避免支付模块与后台模块产生循环依赖
    from routers import pay
    # 预计耗时按本单实际打印页数重新估算，避免重印单立刻被判超时免单
    start = int(job.get("print_range_start") or 1)
    end = int(job.get("print_range_end") or 1)
    pages = max(end - start + 1, 1) * int(job.get("print_copies") or 1)
    eta = delivery.estimate_eta_seconds(job.get("printer_id"), pages)
    out_trade_no = (job.get("out_trade_no") or f"ORDER{order_id}") + "_R"
    file_attributes = {
        "folder": folder,
        # 实际读取的源文件（重印副本或原会话目录），入队时按此定价/搬移
        "filename": pdf_name,
        "source_path": source_path,
        "display_name": display,
        "print_copies": job.get("print_copies") or 1,
        "print_range_start": job.get("print_range_start") or 1,
        "print_range_end": job.get("print_range_end") or 1,
        "print_side": job.get("print_side") or "one-sided",
        "paper": job.get("paper") or "A4",
        "print_color": job.get("print_color") or "bw",
        "pickup_code": job.get("pickup_code"),
        "printer_id": job.get("printer_id"),
        # 重印订单仍归原下单账号
        "user_id": job.get("user_id"),
        # 重印不计费：本单实付 0，原单金额记为「已省」
        "amount": 0,
        "saved_amount": job.get("amount"),
        "reprint_of": order_id,
        "reprint_reason": job.get("reprint_reason"),
    }
    if job.get("batch"):
        # 保持原单的批量 / 拼单封皮方式：按每份正文页数与原始份数重新拼包
        file_attributes["batch"] = {
            "mode": job.get("batch_mode") or "unified",
            "names": job.get("batch_names") or [],
            "owner": job.get("batch_owner") or "",
        }
        if job.get("batch_pages") and job.get("batch_copies"):
            file_attributes["print_copies"] = job.get("batch_copies")
            file_attributes["print_range_start"] = 1
            file_attributes["print_range_end"] = job.get("batch_pages")
    pay._enqueue_paid_order([file_attributes], out_trade_no=out_trade_no, eta=eta)
    db.set_reprint_status(order_id, "approved")
    return ReturnResult(200, "success", {
        "order_id": order_id,
        "reprint_status": "approved",
        "out_trade_no": out_trade_no,
    })


@router.post("/api/order/{order_id}/reprint/reject")
def reprint_reject(order_id: int, request: ReprintReview,
                   role: str = Depends(verify_merchant_key)):
    """驳回重印申请（例如并非打印质量问题，或需用户重新上传）。"""
    job = db.get_job_by_id(order_id)
    if job is None:
        return ReturnResult(200, "订单不存在", {})
    if job.get("reprint_status") != "requested":
        return ReturnResult(200, "该订单没有待处理的重印申请", {})
    db.set_reprint_status(order_id, "rejected")
    return ReturnResult(200, "success",
                        {"order_id": order_id, "reprint_status": "rejected"})


@router.get("/api/price")
def price_current(device_id: Optional[str] = None):
    """用户端读取生效单价（无需密钥）。

    传 device_id 时返回「该台机器」的实际生效价（该机独立价或全局价），
    不传则返回全局价。用户端选机后按此覆盖本地默认价，
    保证「前端显示价 = 后端结算价」。
    """
    data = db.effective_price(device_id)
    # 彩色按覆盖率分档的阈值/系数，供用户端预估与展示
    data["coverage"] = pricing.coverage_config()
    return ReturnResult(200, "success", data)


@router.post("/api/price/config")
def price_set(request: PriceSettings, role: str = Depends(verify_merchant_key)):
    """管理端修改「全局」打印单价（元/页）。改完立即对结算与用户端显示生效。

    未单独定价的机器跟随这里的全局价。
    """
    fields = (("bw", request.bw), ("color", request.color),
              ("bw_duplex", request.bw_duplex), ("color_duplex", request.color_duplex))
    if all(value is None for _, value in fields):
        return ReturnResult(200, "nothing to update", {})
    for name, value in fields:
        if value is not None and value <= 0:
            return ReturnResult(200, f"{name} must be > 0", {})
    config = db.set_price_config(request.bw, request.color,
                                 request.bw_duplex, request.color_duplex)
    return ReturnResult(200, "success", config)


@router.get("/api/price/devices")
def price_devices(role: str = Depends(verify_admin_key)):
    """每台机器的生效单价（管理端「每台机器定价」表格）。

    source 为 device 表示该机单独定过价，global 表示跟随全局价。
    """
    return ReturnResult(200, "success", {
        "global": db.get_price_config(),
        "devices": db.list_device_prices(),
    })


@router.post("/api/price/device/{device_id}")
def price_device_set(device_id: str, request: PriceSettings,
                     role: str = Depends(verify_merchant_key)):
    """给单台机器单独定价（元/页）；只传一项时另一项保持原值。"""
    if db.get_device(device_id) is None:
        return ReturnResult(200, "device not found", {})
    fields = (("bw", request.bw), ("color", request.color),
              ("bw_duplex", request.bw_duplex), ("color_duplex", request.color_duplex))
    if all(value is None for _, value in fields):
        return ReturnResult(200, "nothing to update", {})
    for name, value in fields:
        if value is not None and value <= 0:
            return ReturnResult(200, f"{name} must be > 0", {})
    db.set_device_price(device_id, request.bw, request.color,
                        request.bw_duplex, request.color_duplex)
    return ReturnResult(200, "success", {
        "device_id": device_id,
        "price": db.effective_price(device_id),
    })


@router.delete("/api/price/device/{device_id}")
def price_device_clear(device_id: str, role: str = Depends(verify_merchant_key)):
    """清除单台机器的独立定价，使其恢复跟随全局单价。"""
    removed = db.clear_device_price(device_id)
    return ReturnResult(200, "success" if removed else "no device price",
                        {"device_id": device_id,
                         "price": db.effective_price(device_id)})


@router.get("/api/pay/info")
def pay_info():
    """用户端读取收款方式（无需密钥）：个人微信收款码 + 收款人昵称。

    qr_available 由后端核对图片是否真的存在，避免前端展示一张打不开的收款码。
    """
    config = db.get_pay_config()
    return ReturnResult(200, "success", {
        "method": "wechat_personal",
        "label": "微信扫码支付",
        "wechat_qr": config.get("wechat_qr"),
        "payee": config.get("payee"),
        "qr_available": _qr_file_exists(config.get("wechat_qr")),
    })


@router.post("/api/pay/config")
def pay_config_set(request: PaySettings, role: str = Depends(verify_merchant_key)):
    """管理端修改收款配置：收款码图片路径 / 收款人昵称。"""
    if request.wechat_qr is None and request.payee is None:
        return ReturnResult(200, "nothing to update", {})
    config = db.set_pay_config(request.wechat_qr, request.payee)
    return ReturnResult(200, "success", dict(config,
                                             qr_available=_qr_file_exists(config.get("wechat_qr"))))


@router.get("/api/pay/list")
def pay_list(role: str = Depends(verify_merchant_key)):
    """收款流水 + 汇总：管理端看「收了多少、还有多少待核验」。"""
    return ReturnResult(200, "success", {
        "payments": db.list_payments(),
        "summary": db.payment_summary(),
    })


@router.post("/api/pay/{payment_id}/confirm")
def pay_confirm(payment_id: int, role: str = Depends(verify_merchant_key)):
    """管理端核对到账后，把一条「待核验」流水标记为「已收款」。"""
    changed = db.confirm_payment(payment_id)
    return ReturnResult(200, "success" if changed else "already confirmed",
                        {"payment_id": payment_id})


@router.get("/api/price/compare")
def price_compare(role: str = Depends(verify_admin_key)):
    """价格对比数据（思印客当前定价 vs PaperCut），用于管理端展示。"""
    config = db.get_price_config()
    us_bw = config["bw"]
    us_color = config["color"]
    us_bw_duplex = config["bw_duplex"]
    us_color_duplex = config["color_duplex"]
    pc_bw = 0.5
    pc_color = 1.0
    pc_bw_duplex = 0.8
    pc_color_duplex = 1.8
    ratio = us_bw / pc_bw * 10 if pc_bw else 0
    ratio_text = f"{ratio:.1f}".rstrip("0").rstrip(".")
    data = {
        "currency": "CNY",
        "config": config,
        "us": {
            "name": "思印客 Silnk",
            "bw_per_page": us_bw,
            "color_per_page": us_color,
            "bw_duplex_per_sheet": us_bw_duplex,
            "color_duplex_per_sheet": us_color_duplex,
            "min_charge": us_bw,
            "payment": "扫码支付，按文件即时计价",
        },
        "papercut": {
            "name": "PaperCut",
            "bw_per_page": pc_bw,
            "color_per_page": pc_color,
            "bw_duplex_per_sheet": pc_bw_duplex,
            "color_duplex_per_sheet": pc_color_duplex,
            "min_charge": pc_bw,
            "payment": "需预充值账户体系",
        },
        "summary": f"同样黑白 A4 单页，思印客 {us_bw} 元，约为 PaperCut 的 {ratio_text} 折，且无需预充值。",
    }
    return ReturnResult(200, "success", data)


@router.get("/api/stats/today")
def stats_today(role: str = Depends(verify_admin_key)):
    """今日各小时打印页数（只统计已完成订单，用于管理端趋势图）。

    页数口径：每单（打印范围结束 - 开始 + 1）× 份数；后端按服务器本地时间分桶。
    """
    return ReturnResult(200, "success", db.today_stats())


def _date_error(value):
    """校验 YYYY-MM-DD，非法返回错误文案，合法返回 None。"""
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return None
    except ValueError:
        return "日期格式应为 YYYY-MM-DD"


def _csv_cell(value):
    """防 CSV 公式注入：以 = + - @ 或控制字符开头的文本前加单引号。

    设备名/主机名来自未鉴权配对申请，不能直接交给 Excel 求值。
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_response(filename, header, rows):
    """生成带 BOM 的 CSV（Excel 直接双击不乱码）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows([[_csv_cell(cell) for cell in row] for row in rows])
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="%s"' % filename},
    )


@router.get("/api/reports/summary")
def report_summary(start: str = "", end: str = "", tenant_id: str = "",
                   device_id: str = "", role: str = Depends(verify_merchant_key)):
    """统计报表：汇总、按天、按设备、按租户（运营密钥不可见金额）。"""
    error = _date_error(start) or _date_error(end)
    if error:
        return ReturnResult(200, error, {})
    return ReturnResult(200, "success", db.report_summary(
        start=start or None, end=end or None,
        tenant_id=tenant_id or None, device_id=device_id or None))


@router.get("/api/reports/daily.csv")
def report_daily_csv(start: str = "", end: str = "", tenant_id: str = "",
                     device_id: str = "", role: str = Depends(verify_merchant_key)):
    """导出按天汇总的 CSV。"""
    error = _date_error(start) or _date_error(end)
    if error:
        return ReturnResult(200, error, {})
    data = db.report_summary(start=start or None, end=end or None,
                             tenant_id=tenant_id or None,
                             device_id=device_id or None)
    rows = [[d["date"], d["orders"], d["completed"], d["pages"], d["revenue"]]
            for d in data["daily"]]
    name = "silnk_daily_%s_%s.csv" % (start or "all", end or "all")
    return _csv_response(name, ["日期", "订单数", "完成数", "打印页数", "营收(元)"], rows)


@router.get("/api/reports/orders.csv")
def report_orders_csv(start: str = "", end: str = "", tenant_id: str = "",
                      device_id: str = "", role: str = Depends(verify_merchant_key)):
    """导出订单明细 CSV，便于在 Excel 里自行透视。"""
    error = _date_error(start) or _date_error(end)
    if error:
        return ReturnResult(200, error, {})
    jobs = db.report_jobs(start=start or None, end=end or None,
                          tenant_id=tenant_id or None,
                          device_id=device_id or None)
    names = {d["device_id"]: (d.get("name") or d["device_id"])
             for d in db.list_devices()}
    rows = []
    for job in jobs:
        rows.append([
            job.get("id"),
            job.get("created_at") or "",
            job.get("status") or "",
            job.get("printer_id") or "",
            names.get(job.get("printer_id"), job.get("printer_id") or ""),
            job.get("tenant_id") or "default",
            job.get("user_id") or "",
            job.get("print_copies") or 1,
            db._job_total_pages(job),
            {"one-sided": "单面", "two-sided-long-edge": "双面长边",
             "two-sided-short-edge": "双面短边"}.get(
                 str(job.get("print_side") or ""), job.get("print_side") or ""),
            "彩色" if job.get("print_color") == "color" else "黑白",
            job.get("paper") or "A4",
            job.get("amount") or 0,
            job.get("saved_amount") or 0,
            "是" if job.get("waived") else "否",
            job.get("out_trade_no") or "",
        ])
    name = "silnk_orders_%s_%s.csv" % (start or "all", end or "all")
    return _csv_response(name, [
        "订单ID", "创建时间", "状态", "设备ID", "设备名称", "租户", "用户ID",
        "份数", "总页数（含份数）", "单双面", "色彩", "纸张", "金额(元)", "优惠(元)",
        "是否免单", "支付单号"], rows)


@router.get("/api/file/deletions")
def file_deletions(limit: int = 100, role: str = Depends(verify_admin_key)):
    """文件删除审计记录（最新在前）。"""
    limit = max(1, min(int(limit or 100), 500))
    return ReturnResult(200, "success", {"deletions": db.list_file_deletions(limit)})


@router.get("/api/status")
def project_status(role: str = Depends(verify_admin_key)):
    """接入状态：如实区分「已接入」与「待做」。

    说明（不编造）：
    - runtime 段是运行时从数据库读出的事实；
    - declared 段是人工维护的接入声明，凡是尚未接入的能力一律标「待做」，
      不假装可用。后续接入打印机/传感器后，把对应条目的 status 改为「已接入」。
    """
    devices = db.list_devices()
    return ReturnResult(200, "success", {
        "runtime": {
            "devices_total": len(devices),
            "devices_with_heartbeat": len([d for d in devices if d.get("last_heartbeat")]),
            "jobs_total": len(db.list_jobs()),
            "alerts_total": len(db.list_alerts()),
        },
        "declared": [
            {
                "key": "printer_discovery",
                "title": "打印机队列自动发现（Windows / CUPS）",
                "status": "已接入",
                "note": "客户端启动与心跳时用 Win32_Printer 或 lpstat 枚举本机队列，"
                        "过滤 PDF/传真/OneNote 等虚拟打印机，管理端「新增设备」可一键填入名称/驱动/端口。"
                        "任何装了系统驱动或 CUPS 队列的型号都能接；实际打印仍由 SumatraPDF/CUPS 完成，"
                        "型号只作展示，不做型号白名单限制。",
            },
            {
                "key": "user_frontend",
                "title": "用户端页面",
                "status": "已接入",
                "note": "根路径 / 即用户端自助下单页，用的是基座自带 Vue front（Vue3+Vite）构建产物 static/app/dist，"
                        "已按「白底清爽 + 蓝色点缀」皮肤改造并新增「选择打印机」区块"
                        "（管理端仍保留 V1 深蓝商务皮肤）。"
                        "流程：选机 /api/printer/available → 拉取单价 /api/price → 上传 /uploadfile → 设置与预估 → 下单 /pay/bill/create。"
                        "管理端为多页站点 static/admin，入口 /ui（307 跳 /admin/）。",
            },
            {
                "key": "pricing_config",
                "title": "打印单价管理端可改（含每台机器独立定价）",
                "status": "已接入",
                "note": "全局单价保存在 price_config 表，默认单面黑白 0.3 元/页、彩色 0.8 元/页，"
                        "双面黑白 0.5 元/张、彩色 1.5 元/张（双面奇数页时最后一页按单面价），"
                        "管理端「价格对比」页可改（POST /api/price/config）；"
                        "每台机器还可单独定价，存在 device_price 表，"
                        "通过 POST /api/price/device/{device_id} 设置、DELETE 同路径恢复跟随全局。"
                        "结算 /pay/bill/create 按「该机生效价」计算，用户端选机后经 "
                        "GET /api/price?device_id=xxx 拉取同一份价（无需密钥），因此不存在前后端价格不一致。",
            },
            {
                "key": "wechat_personal_pay",
                "title": "个人微信收款码收款",
                "status": "已接入" if _qr_file_exists(db.get_pay_config().get("wechat_qr")) else "待配置",
                "note": (
                    "当前收款方式为个人微信收款码（无商户号）：用户下单后弹出收款码与应付金额，"
                    "扫码付款后点「我已支付」调 POST /pay/wechat/submit，订单立即入队。"
                    "收款码路径与收款人昵称存 pay_config 表，管理端「账单流水」页可改（POST /api/pay/config），"
                    "图片经 /pay-qr 静态挂载对外提供。"
                    + (
                        "收款码图片已存在，用户端可正常扫码。"
                        "注意：个人收款码拿不到微信到账回调，后端无法自动确认收款，"
                        "流水先记「待核验」，由管理端在账单流水页人工确认。"
                        if _qr_file_exists(db.get_pay_config().get("wechat_qr"))
                        else "当前 pay_config 指向的收款码图片在服务器上不存在，用户端会如实提示收款码未配置。"
                    )
                ),
            },
            {
                "key": "billing_ledger",
                "title": "账单流水（收了多少钱）",
                "status": "已接入",
                "note": "每笔订单提交后写一条收款流水到 payments 表（订单号、金额、设备、计费页数、耗纸张数、"
                        "文件摘要、付款备注、时间），管理端「账单流水」页（GET /api/pay/list）展示明细与汇总："
                        "已核验收款额、待核验金额、今日收款额、笔数。"
                        "核对到账后点「确认收款」调 POST /api/pay/{id}/confirm 记为已收款。"
                        "汇总口径如实区分「已核验到账」与「用户声明已付待核验」，不把后者算成已收款。",
            },
            {
                "key": "wecom_webhook",
                "title": "企微群机器人告警推送",
                "status": "已接入" if notify.is_configured() else "待配置",
                "note": (
                    "告警引擎每新增一条告警（R1~R5，去重后）就异步推一条文本消息到企微群机器人，"
                    "推送在后台线程发送，不会拖住 10 秒一轮的扫描；"
                    "管理端告警页可点「测试推送」用 POST /api/alert/test-push 现场验证。"
                    + (
                        "当前已配置 Webhook（环境变量 WECOM_WEBHOOK_URL），推送会真实发出。"
                        if notify.is_configured()
                        else "当前未配置环境变量 WECOM_WEBHOOK_URL，因此不会真实推送；"
                             "测试接口会如实返回 push failed / webhook not configured。"
                    )
                ),
            },
            {
                "key": "office_convert",
                "title": "Word / Excel → PDF",
                "status": "已接入",
                "note": "改用 LibreOffice headless（系统命令 soffice，无需图形界面），"
                        "已实测 docx / xlsx 转 PDF 成功并正确回读页数。"
                        "要求服务器装有 libreoffice-writer / libreoffice-calc。",
            },
            {
                "key": "paper_sensor",
                "title": "纸量硬件感知",
                "status": "待做",
                "note": "普通打印机无纸量传感器。R1 为软件估算：按订单实际耗纸张数累计"
                        "（单面 n 页 = n 张，双面 n 页 = ceil(n/2) 张，再乘份数），"
                        "paper_left = 纸盒容量 − 累计用纸张数，并结合四档状态校准。并非硬件实测。"
                        "管理端设备页可设「纸盒容量 / 当前剩余张数」，设剩余即按实际值校准。",
            },
            {
                "key": "ups_runtime",
                "title": "UPS 剩余供电分钟数",
                "status": "待做",
                "note": "心跳接口已预留 ups_runtime_min 字段；设备未上报前，R3 消息不显示分钟数。",
            },
            {
                "key": "door_sensor",
                "title": "门磁传感器（R4）",
                "status": "待做",
                "note": "心跳接口已预留 door_open 字段；无硬件时该规则不参与判定。",
            },
            {
                "key": "pickup_sensor",
                "title": "取件顺序传感器（R5）",
                "status": "待做",
                "note": "心跳接口已预留 pickup_seq 字段；无硬件时该规则不参与判定。",
            },
            {
                "key": "alipay",
                "title": "支付宝当面付",
                "status": "待做",
                "note": "APP_ID / 公钥 / 私钥均为空，支付宝真实收款未接入。"
                        "当前收款不依赖支付宝，走「个人微信收款码」通道（见 wechat_personal_pay 条目）；"
                        "一旦配好支付宝密钥，/pay/bill/create 会自动改走支付宝预下单并返回支付链接，"
                        "/pay/return 回调仍是原有入队逻辑。",
            },
            {
                "key": "admin_roles",
                "title": "管理端角色分离（商家 / 运维）",
                "status": "已接入",
                "note": "商家密钥可改价格、看账单、确认收款；运维密钥只能看设备/告警/订单状态、"
                        "打印测试页、重启客户端。运维登录后侧栏隐藏订单/账单/价格，"
                        "直接打开商家页面会被退回概览；后端对改价、账单、收款、删设备返回 403。"
                        "运维密钥由环境变量 OPS_KEY 配置，未配置时运维登录关闭。",
            },
            {
                "key": "docker_compose",
                "title": "Docker Compose 环境",
                "status": "待做",
                "note": "目标服务器未安装 Docker；当前以后端代码 + systemd 方式部署。",
            },
            {
                "key": "pickup_code",
                "title": "取件码（只在用户端展示）",
                "status": "已接入",
                "note": "下单时生成 6 位取件码，只在用户端订单详情展示，"
                        "不打印在纸上（每页与批量封皮都不印），避免多耗墨粉、也便于纸张复用。"
                        "如实说明：没有储物柜/门锁，取件码只能防拿错，不能阻止别人提前拿走已出的纸。",
            },
            {
                "key": "scheduled_print",
                "title": "定时打印（预约出纸）",
                "status": "已接入",
                "note": "用户下单时可勾选「定时出纸」并选时间（最少提前 1 分钟、最多 7 天内）："
                        "付款后订单落库、待打印文件已备好，但不进打印队列（status=scheduled）；"
                        "后台每 30 秒扫描一次，到点自动改成 pending 并放行给打印机端；"
                        "到点后 12 小时仍无人打印则作废（expired），避免隔天突然出纸。"
                        "未到点前用户可在订单详情里「取消预约」，已付款的收款状态如实记为"
                        "「待退款」，因个人收款码没有自动退款通道，需现场人工退回。",
            },
            {
                "key": "print_precheck",
                "title": "打印前智能体检",
                "status": "已接入",
                "note": "上传后逐页检查并提示：空白页（默认跳过、不计费）、彩色页与当前黑白设置不符、"
                        "内容贴边可能被裁切、横向页翻面方向、图片有效分辨率过低（<150 DPI 会发糊）、"
                        "页面尺寸不统一（A4/A3/A5/Letter 混排会提示装订不齐）、"
                        "双面打印奇数页导致最后一张背面空白。所有提示只做提醒，不阻断下单。",
            },
            {
                "key": "format_templates",
                "title": "格式合规模板",
                "status": "已接入",
                "note": "内置 7 套模板：通用论文、通用作业、自由格式、学位论文（国标）、实验报告、"
                        "简历、公文（GB/T 9704），下单时在文件卡片上切换。"
                        "自由格式完全跳过格式判定（分数 100、无问题项），"
                        "且一键修复只做内容级操作（删空白页/插封面/双面配对），不动页边距与页码。"
                        "模板边距/行距为校园常见要求，非某校官方文件；"
                        "可用 templates/*.json 扩展，新增学校模板无需改代码。",
            },
            {
                "key": "shared_library",
                "title": "共享资料库（agent + 管理员双审核）",
                "status": "已接入",
                "note": "用户下单时可自愿勾选「共享到公开资料库」，勾选后本单立减 5%；"
                        "用户已公开资料的累计打印量还会带来贡献折扣（1/5/20/50 次对应 3%/5%/8%/10%），"
                        "与共享 5% 取较高值。只复制本次实际打印范围的成品 PDF，不共享原始文件，班级批量禁止共享。"
                        "付款入队后先跑 agent 初审（DeepSeek 文本审核 + 本地身份证号/手机号扫描；"
                        "未配置 DeepSeek 时回退 GLM），再转商家管理员二次复审，通过后才公开。"
                        "未接入或调用失败时如实标记为「未接入 / 调用失败」并转人工，"
                        "不假装通过；管理员可预览后通过或驳回，驳回原因不对外展示。",
            },
            {
                "key": "ops_agent",
                "title": "运维 agent（按故障类型分档处置）",
                "status": "已接入",
                "note": "后台每 20 秒跑一轮，只做两类事。"
                        "① 物理故障（缺纸 R1 / 离线 R2 / 断电 R3 / 门磁卡纸 R4 / 取件顺序 R5）："
                        "软件解决不了，只把告警翻译成现场处置动作并落库留痕（action_status=reported），"
                        "企微推送仍由告警引擎负责，agent 不重复推送，人工到现场处理。"
                        "② 打印链路假死：设备心跳正常但订单卡在 printing 超 5 分钟，"
                        "判定是 CUPS/ipptool 卡住而非设备掉线，自动下发 restart 指令自愈；"
                        "重启不续打已出队的任务，因此同时把该单如实标 failed 并推企微，用户可申请重印。"
                        "边界：设备离线（心跳超时）时命令下发不到，不自动重启，归入①人工。"
                        "同一设备 30 分钟内最多自愈一次，防止把 agent 自己卷进重启循环。"
                        "留痕接口 GET /api/agent/runs（可带 device_id、limit），"
                        "手动跑一轮 POST /api/agent/scan，均需管理端密钥。",
            },
            {
                "key": "privacy_retention",
                "title": "文件自动删除与删除记录",
                "status": "已接入",
                "note": "未付款上传会话 15 分钟不活跃自动删除；用户点删除时立即清理；"
                        "订单打印完成后 5 分钟清理 uploads 下的"
                        "原文件、转换文件、修复备份与 OCR 缓存；失败/完成残留的待打印副本 24 小时清理；"
                        "每次删除都写 file_deletions 表，本页下方可查。",
            },
            {
                "key": "multi_tenant",
                "title": "多租户与软件订阅",
                "status": "已接入",
                "note": "一个租户 = 一个客户（打印点 / 学校文印室）。平台方用商家密钥调 "
                        "POST /api/tenant/create 开通租户，返回每租户独立密钥（可交给客户）；"
                        "设备/订单/告警均带 tenant_id，客户端用环境变量 TENANT_ID 上报归属；"
                        "单价按「设备独立价 > 租户默认价 > 平台全局价」三级生效；"
                        "租户用独立页面 /tenant/ 输入自己的租户密钥登录，只看/改本租户的"
                        "订阅、设备、订单、告警与默认价，平台级接口一律拒绝租户密钥"
                        "（role_of_key 返回 None）。"
                        "本阶段为「手动开通、手动续期」，未接支付网关、无自助开通、无自动计费；"
                        "自助开通、自动计费为「待做」；租户在页面上自助清除本租户价、"
                        "恢复跟随平台全局价也为「待做」，当前需由平台方清除。",
            },
            {
                "key": "device_privacy",
                "title": "打印机端最小化可见",
                "status": "已接入",
                "note": "下发给打印机的任务只含 folder / 订单号文件名 / 页范围 / 份数 / 单双面 / 纸张 / 色彩 / 设备，"
                        "不含真实文件名、金额、批量名单；客户端的成功日志也只记订单号文件名。",
            },
        ],
    })
