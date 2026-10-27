"""告警引擎。

按 PRD 定义的告警规则扫描设备与打印队列，生成告警记录：
  R1 纸量预警：估算剩余纸量 ≤ 10%
  R2 停机告警：心跳超时（离线/断电）
  R3 市电断开：UPS 供电中
  R4 取件异常：门磁开启但仍有未完成订单
  R5 顺序异常：取件顺序与队列顺序不符
  R6 打印失败：本设备有 failed 订单（每张失败单只告警一次，靠 failure_alerted 去重）
  R6 出纸异常：心跳在线、订单卡在 printing 超过 5 分钟、且没有任何出纸事件
     （X5 上报出纸事件后条件解除，告警自动关窗）

R3/R4/R5 依赖真实传感器，可能不存在：设备没上报相关字段时直接跳过，
不产生告警，绝不依赖真实硬件。

去重：同一 rule + device_id 已有未处理告警时不再重复插入（is_alert_open）。
自动关窗：每轮扫描结束，把该设备名下、条件已经恢复的未处理告警自动标记为已处理。
推送：每次真正新增告警时，异步推一条文本消息到企微群机器人（notify.push_alert）；
未配置 WECOM_WEBHOOK_URL 时跳过，不阻塞扫描、也不假装推送成功。
按设备隔离：R4/R5 只统计属于本设备的订单（print_jobs.printer_id），
避免现场多台打印机时互相误报（A 的门磁一开，B 也跟着报 R4）。
"""

from datetime import datetime

import db
import notify

# 心跳超时阈值（秒），超过即判定离线
OFFLINE_TIMEOUT_SECONDS = 60
# 纸量预警阈值（剩余占比）
PAPER_LOW_RATIO = 0.1
# 乱序取件告警保持的时间窗口（秒），过后条件恢复自动关窗
RECENT_SKIP_SECONDS = 600
# 订单卡在 printing 且无出纸事件的判定时长（秒），超时触发 R6 出纸异常
EJECT_MISSING_SECONDS = 300

# 规则编号 → 名称/等级，便于统一维护
RULE_LEVEL = {
    "R1": "warning",
    "R2": "critical",
    "R3": "critical",
    "R4": "warning",
    "R5": "warning",
    "R6": "critical",
}


def _parse_ts(value):
    """把数据库时间戳字符串解析为 datetime，失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _is_offline(device):
    """心跳超时判定。从未上报过心跳的设备跳过（避免误报）。"""
    last = _parse_ts(device.get("last_heartbeat"))
    if last is None:
        return False
    return (datetime.now() - last).total_seconds() > OFFLINE_TIMEOUT_SECONDS


def _emit(created, rule, device_id, message, triggered_at=None):
    """统一创建告警、做去重并向企微推送，返回是否真的新增。"""
    if db.is_alert_open(rule, device_id):
        return False
    alert_id = db.add_alert(
        rule, device_id, RULE_LEVEL.get(rule, "warning"), message,
        triggered_at=triggered_at,
    )
    alert = {
        "id": alert_id,
        "rule": rule,
        "device_id": device_id,
        "level": RULE_LEVEL.get(rule, "warning"),
        "message": message,
    }
    created.append(alert)
    # 异步推送，不阻塞扫描；未配置 Webhook 时会安静跳过
    notify.push_alert(alert)
    return True


def _r1_decision(device):
    """按优先级判定 R1 是否告警，返回 (是否告警, 告警消息或None)。

    优先级（严格按顺序）：
      1) paper_state 为 empty/low → 直接告警（消息带状态）
      2) paper_state 为 full → 不告警
      3) paper_state 为 half → 用 remaining = paper_capacity/2 算 ratio，≤0.1 才告警
      4) 无 paper_state、有上报 paper_left → ratio = paper_left/paper_capacity
      5) 都没有、但 paper_capacity 非空 → remaining = capacity - 累计已用纸张数，算 ratio
      6) 任一步 ratio ≤ PAPER_LOW_RATIO → 告警（消息区分来源）
    """
    device_id = device.get("device_id")
    paper_state = device.get("paper_state")
    paper_left = device.get("paper_left")
    paper_capacity = device.get("paper_capacity")
    # 软件累计的「已用纸张数」（列名沿用 pages_printed，口径是张：单面 n 页 = n 张，
    # 双面 n 页 = ceil(n/2) 张），见 db.add_sheets_used
    used_sheets = device.get("pages_printed")

    # 1) 状态档位：空纸/少纸 直接告警
    if paper_state in ("empty", "low"):
        label = "空纸" if paper_state == "empty" else "少纸"
        return True, f"打印机 {device_id} 纸张状态：{label}，请及时补纸"
    # 2) 状态档位：满纸 不告警
    if paper_state == "full":
        return False, None
    # 3) 状态档位：半纸 按容量一半估算（正常不会触发，保留逻辑）
    if paper_state == "half":
        if paper_capacity:
            try:
                remaining = float(paper_capacity) / 2
                ratio = remaining / float(paper_capacity)
            except (TypeError, ValueError, ZeroDivisionError):
                ratio = None
            if ratio is not None and ratio <= PAPER_LOW_RATIO:
                return True, (
                    f"打印机 {device_id} 剩余纸量仅 {remaining:.0f}/{paper_capacity}"
                    f"（半纸档估算），请及时补纸"
                )
        return False, None

    # 4)/5) 无状态档位：优先用上报 paper_left，其次按打印量估算
    if paper_left is not None and paper_capacity:
        try:
            ratio = float(paper_left) / float(paper_capacity)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = None
        if ratio is not None and ratio <= PAPER_LOW_RATIO:
            return True, (
                f"打印机 {device_id} 剩余纸量仅 {paper_left}/{paper_capacity}，请及时补纸"
            )
    elif paper_capacity:
        try:
            remaining = float(paper_capacity) - float(used_sheets or 0)
            if remaining < 0:
                remaining = 0.0
            ratio = remaining / float(paper_capacity)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = None
        if ratio is not None and ratio <= PAPER_LOW_RATIO:
            return True, (
                f"打印机 {device_id} 剩余纸量仅 {remaining:.0f}/{paper_capacity} 张"
                f"（按软件累计用纸量估算），请及时补纸"
            )
    return False, None


def _r3_message(device):
    """R3 市电断开的告警消息。

    UPS 剩余供电时长需要设备通过 ups_runtime_min 字段上报；
    设备没上报该字段时，消息里只写「待设备上报」，绝不编造具体分钟数。
    """
    device_id = device.get("device_id")
    runtime = device.get("ups_runtime_min")
    try:
        runtime = int(runtime)
    except (TypeError, ValueError):
        runtime = None
    if runtime is not None:
        return f"打印机 {device_id} 市电断开，UPS 供电中，剩余 {runtime} 分钟"
    return f"打印机 {device_id} 市电断开，UPS 供电中（剩余时长待设备上报）"


def evaluate():
    """扫描所有设备与队列，生成告警并把已恢复的告警自动关窗。

    返回本次新增的告警列表（自动关窗不算新增）。
    """
    created = []
    devices = db.list_devices()
    jobs = db.list_jobs()

    for device in devices:
        device_id = device.get("device_id")
        if not device_id:
            continue

        # 只统计属于本设备的订单；printer_id 为空（未指定打印机/旧订单）不计入，
        # 否则多台打印机时 R4/R5 会互相误报
        device_open_jobs = [
            job for job in jobs
            if job.get("printer_id") == device_id and job.get("status") != "completed"
        ]
        device_completed_count = len([
            job for job in jobs
            if job.get("printer_id") == device_id and job.get("status") == "completed"
        ])

        # 本轮仍然成立的规则集合，用于自动关窗
        active_rules = set()

        # R1 估算剩余纸量 ≤ 10% → 补纸预警（四档状态校准 + 打印量估算）
        r1_alert, r1_message = _r1_decision(device)
        if r1_alert:
            active_rules.add("R1")
            _emit(created, "R1", device_id, r1_message)

        # R2 心跳超时 → 停机告警
        if _is_offline(device):
            active_rules.add("R2")
            _emit(
                created, "R2", device_id,
                f"打印机 {device_id} 心跳超时（超过 {OFFLINE_TIMEOUT_SECONDS} 秒无心跳），疑似离线或断电",
            )

        # R3 UPS 市电断开（依赖 ups_online，未上报则跳过）
        ups_online = device.get("ups_online")
        if ups_online is not None and not ups_online:
            active_rules.add("R3")
            _emit(created, "R3", device_id, _r3_message(device))

        # R4 门磁开启但本设备仍有未完成订单（依赖 door_open，未上报则跳过）
        door_open = device.get("door_open")
        if door_open and device_open_jobs:
            active_rules.add("R4")
            _emit(
                created, "R4", device_id,
                f"打印机 {device_id} 门磁开启但仍有 {len(device_open_jobs)} 个未完成订单，疑似取件异常",
            )

        # R5 取件顺序不符（依赖 pickup_seq，未上报则跳过）
        pickup_seq = device.get("pickup_seq")
        if pickup_seq is not None and device_open_jobs:
            # 合法的下一个取件序号 = 本设备已完成订单数 + 1
            expected_seq = device_completed_count + 1
            try:
                actual_seq = int(pickup_seq)
            except (TypeError, ValueError):
                actual_seq = None
            if actual_seq is not None and actual_seq != expected_seq:
                active_rules.add("R5")
                _emit(
                    created, "R5", device_id,
                    f"打印机 {device_id} 取件顺序异常：当前取件序号 {actual_seq}，期望 {expected_seq}",
                )

        # R5 乱序取件（屏幕取件确认产生）：取走时跳过了前面未取走的订单
        skipped_picks = db.recent_skipped_pickups(device_id, RECENT_SKIP_SECONDS)
        if skipped_picks:
            active_rules.add("R5")
            latest = skipped_picks[-1]
            order_no = latest.get("out_trade_no") or f"#{latest.get('id')}"
            _emit(
                created, "R5", device_id,
                f"打印机 {device_id} 乱序取件：订单 {order_no} 取走时跳过了 "
                f"{latest.get('pickup_skipped')} 个先出纸的订单（取件序号 {latest.get('pickup_seq')}）",
            )

        # R6 打印失败：本设备有 failed 订单，且这张单还没有告警过
        failed_jobs = [
            job for job in jobs
            if job.get("printer_id") == device_id and job.get("status") == "failed"
        ]
        if failed_jobs:
            # 有失败单时把 R6 视为本轮仍成立，避免自动关窗把刚生成的告警立刻关掉
            active_rules.add("R6")
            for job in failed_jobs:
                if job.get("failure_alerted"):
                    continue
                order_no = job.get("out_trade_no") or f"#{job.get('id')}"
                message = (f"打印机 {device_id} 订单 {order_no} 打印失败，"
                           f"请检查纸张/卡纸/连接后重试")
                if _emit(created, "R6", device_id, message):
                    db.mark_job_failure_alerted(job.get("id"))

        # R6 出纸异常：心跳在线、订单卡在 printing 超过 5 分钟、且没有任何出纸事件。
        # 只在设备在线时判定，离线已由 R2 报出，避免重复归因。
        if not _is_offline(device):
            stuck_jobs = db.printing_jobs_without_eject(
                device_id, EJECT_MISSING_SECONDS)
            if stuck_jobs:
                active_rules.add("R6")
                job = stuck_jobs[0]
                order_no = job.get("out_trade_no") or f"#{job.get('id')}"
                extra = (f"（另 {len(stuck_jobs) - 1} 单同样未出纸）"
                         if len(stuck_jobs) > 1 else "")
                _emit(
                    created, "R6", device_id,
                    f"打印机 {device_id} 出纸异常：订单 {order_no} 已打印 "
                    f"{EJECT_MISSING_SECONDS // 60} 分钟仍无出纸检测事件{extra}，"
                    f"请检查是否卡纸、缺纸或被暂停",
                )

        # 自动关窗：本设备名下、条件已恢复的未处理告警一律关闭
        db.resolve_device_alerts_except(device_id, active_rules)

    return created
