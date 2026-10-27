"""取件屏（T5 开发板）只读状态接口。

GET /api/display/{device_id}/state
鉴权与打印机端一致：商家密钥，或该设备自己的设备令牌（别的设备令牌返回 403）。
所有字段都来自数据库真实数据，不做任何模拟：
  - price  ：db.effective_price(device_id)，与结算、用户端同一份价（设备价 > 租户价 > 全局价）
  - paper  ：devices.paper_left
  - queue  ：该设备 status 为 pending / printing 的订单数
  - state  ：busy = 队列里有未完成订单；
             pickup = 无未完成订单，且最近一单在 PICKUP_WINDOW_SEC 秒内打印完成并带取件码；
             否则 idle
"""

import threading
import time
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import db
from jwt import auth_device_id, verify_device_or_merchant

router = APIRouter()

PICKUP_WINDOW_SEC = 300

# 轮询限流：同一设备在 WINDOW 秒内最多打 MAX_REQUESTS 次，超出返回 429。
# 屏幕固件正常 3 秒一取，10 秒 20 次只在写错死循环时才会出现。
DISPLAY_RATE_LIMIT = "10/10s"
RATE_WINDOW_SEC = 10
RATE_MAX_REQUESTS = 20

_rate_lock = threading.Lock()
_rate_hits = {}


def _rate_limited(device_id, now=None):
    """按设备滑动窗口计数；超限返回 (True, 需要等待的秒数)。"""
    now = time.monotonic() if now is None else now
    with _rate_lock:
        hits = [t for t in _rate_hits.get(device_id, []) if now - t < RATE_WINDOW_SEC]
        if len(hits) >= RATE_MAX_REQUESTS:
            _rate_hits[device_id] = hits
            return True, max(1, int(RATE_WINDOW_SEC - (now - hits[0])))
        hits.append(now)
        _rate_hits[device_id] = hits
        return False, 0


def _queue_head(device_id, limit=3):
    """队列表头：最多 3 条未完成订单，供屏幕展示标题与页数。"""
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, display_name, filename, pickup_code, status FROM print_jobs"
            " WHERE printer_id=? AND status IN ('pending', 'printing')"
            " ORDER BY id ASC LIMIT ?",
            (device_id, limit))
        return [{
            "order_no": str(row["id"]),
            "title": row["display_name"] or row["filename"] or "",
            "pages": None,
            "pickup_code": row["pickup_code"] or "",
        } for row in cur.fetchall()]
    finally:
        conn.close()


def _paper_level(sheets):
    if sheets is None:
        return "unknown"
    if sheets <= 0:
        return "empty"
    if sheets <= 20:
        return "low"
    return "ok"


def _open_queue(device_id):
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM print_jobs"
            " WHERE printer_id=? AND status IN ('pending', 'printing')",
            (device_id,))
        return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _latest_pickup(device_id):
    conn = db._connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pickup_code, status_at FROM print_jobs"
            " WHERE printer_id=? AND status='completed'"
            " AND pickup_code IS NOT NULL AND pickup_code != ''"
            " AND picked_at IS NULL"
            " ORDER BY id DESC LIMIT 1",
            (device_id,))
        return cur.fetchone()
    finally:
        conn.close()


def _age_seconds(stamp):
    try:
        done = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.now() - done).total_seconds()))


class PickupConfirm(BaseModel):
    code: str


@router.post("/api/display/{device_id}/pickup")
def display_pickup(device_id: str, body: PickupConfirm,
                   scope: str = Depends(verify_device_or_merchant)):
    """取件确认：屏幕触摸或串口回行 {"a":"pick","c":"5218"} 经桥接转到这里。

    幂等：同一取件码重复确认只返回第一次的结果（duplicate=true），不产生第二条记录。
    乱序取件（跳过了前面还没取走的订单）会在 skipped 中体现，并由告警引擎生成 R5。
    """
    token_device = auth_device_id(scope)
    if token_device and token_device != device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)

    limited, retry_after = _rate_limited(device_id)
    if limited:
        return JSONResponse({"message": "too many requests", "device_id": device_id},
                            status_code=429, headers={"Retry-After": str(retry_after)})

    if db.get_device(device_id) is None:
        return JSONResponse({"message": "device not found"}, status_code=404)

    code = (body.code or "").strip()
    if not code or len(code) > 16:
        return JSONResponse({"message": "invalid code"}, status_code=400)

    result = db.confirm_pickup(device_id, code, window_seconds=PICKUP_WINDOW_SEC)
    if result is None:
        return JSONResponse({"message": "pickup code not found"}, status_code=404)
    return {"device_id": device_id, "code": code, "order_id": result["order_id"],
            "duplicate": result["duplicate"], "pickup_seq": result["pickup_seq"],
            "skipped": result["skipped"], "picked_at": result["picked_at"]}


@router.get("/api/display/{device_id}/state")
def display_state(device_id: str, scope: str = Depends(verify_device_or_merchant)):
    token_device = auth_device_id(scope)
    if token_device and token_device != device_id:
        return JSONResponse({"message": "设备令牌与 device_id 不匹配"}, status_code=403)

    limited, retry_after = _rate_limited(device_id)
    if limited:
        print("display rate limited: %s" % device_id)
        return JSONResponse({"message": "too many requests", "device_id": device_id},
                            status_code=429, headers={"Retry-After": str(retry_after)})

    device = db.get_device(device_id)
    if device is None:
        return JSONResponse({"message": "device not found"}, status_code=404)

    queue_len = _open_queue(device_id)
    state, code = "idle", ""
    if queue_len > 0:
        state = "busy"
    else:
        row = _latest_pickup(device_id)
        if row is not None:
            age = _age_seconds(row["status_at"])
            if age is not None and age <= PICKUP_WINDOW_SEC:
                state, code = "pickup", str(row["pickup_code"])

    price = db.effective_price(device_id)
    sheets = device.get("paper_left")
    return {
        "device_id": device_id,
        "ts": int(time.time()),
        "state": state,
        "queue": _queue_head(device_id),
        "queue_len": queue_len,
        "paper": {"level": _paper_level(sheets), "sheets": sheets},
        "pickup": {"code": code},
        "price": {"bw": price.get("bw"), "color": price.get("color"),
                  "a4_bw": price.get("bw"), "source": price.get("source")},
    }
