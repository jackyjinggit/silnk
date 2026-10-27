"""交付承诺：预计完成时间与超时免单。

下单时按「前面排队的页数 + 本次页数 / 该机打印速度」估算预计耗时，
固定加一段预热/取件缓冲，得到承诺完成时间（eta_deadline）。
打印机上报 completed 时若晚于该时间，即按承诺免单。

口径与 /api/printer/recommend 的等待时间估算保持一致，避免两处算法走偏。
"""

from datetime import datetime, timedelta

import db

# 预热 + 出纸 + 用户走到打印机的固定缓冲，单位：分钟
FIXED_BUFFER_MIN = 2.0
# 设备未上报速度时的兜底：10 页/分钟
DEFAULT_SPEED_PPM = 10.0


def _queue_pages(printer_id):
    """该机前面排队的页数：本机的未完成订单 + 未指定设备的共享订单。"""
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
    if not printer_id:
        # 未指定设备：按共享队列估算（由第一台来取任务的设备认领）
        return shared_pages
    return device_pages.get(printer_id, 0) + shared_pages


def _speed_ppm(printer_id):
    """该机打印速度（页/分钟）；未指定设备或未上报时用兜底值。"""
    if printer_id:
        device = db.get_device(printer_id)
        if device:
            try:
                speed = float(device.get("speed_ppm") or DEFAULT_SPEED_PPM)
                if speed > 0:
                    return speed
            except (TypeError, ValueError):
                pass
    return DEFAULT_SPEED_PPM


def estimate_eta_seconds(printer_id, pages):
    """返回 (预计耗时秒数, 承诺完成时间字符串)。

    pages 为本次订单的计费页数；printer_id 为空时按共享队列估算。
    承诺完成时间格式与库内其他时间一致：%Y-%m-%d %H:%M:%S。
    """
    queue_pages = _queue_pages(printer_id)
    speed = _speed_ppm(printer_id)
    try:
        pages = max(int(pages or 0), 0)
    except (TypeError, ValueError):
        pages = 0
    minutes = (queue_pages + pages) / speed + FIXED_BUFFER_MIN
    seconds = max(int(round(minutes * 60)), 60)
    deadline = (datetime.now() + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S")
    return seconds, deadline
