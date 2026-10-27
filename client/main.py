import os
import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from time import sleep
from urllib.parse import quote

import setting

# Windows 主机：不走 ipptool，改用 SumatraPDF 静默打印到本机已安装的打印机队列。
IS_WINDOWS = sys.platform.startswith("win")
# Windows 打印机队列名（控制面板 → 设备和打印机 里的名字）。为空则打到默认打印机。
PRINTER_NAME = os.environ.get("PRINTER_NAME", "")
# SumatraPDF 可执行文件路径；默认从 PATH 查找，也会自动探测两个常见安装目录。
SUMATRAPDF_PATH = os.environ.get("SUMATRAPDF_PATH", "SumatraPDF.exe")
# 强制单面：手动双面打印机（如 HP Smart Tank 518）在双面打印时会弹「翻面后继续」
# 的对话框，无人值守场景会卡住，设 PRINTER_FORCE_SIMPLEX=1 可全部按单面出纸。
FORCE_SIMPLEX = os.environ.get("PRINTER_FORCE_SIMPLEX", "").strip().lower() in (
    "1", "true", "yes", "on")

# 打印失败重试：每份最多再试 2 次（合计 3 次），每次间隔 3 秒。
# 注意：失败可能发生在已出部分纸之后，重试有重复出纸的可能，日志会明确记录。
MAX_PRINT_RETRIES = int(os.environ.get("PRINT_MAX_RETRIES", "2"))
PRINT_RETRY_DELAY_SECONDS = float(os.environ.get("PRINT_RETRY_DELAY", "3"))

# 本机打印机在后端的唯一标识：
# 优先读环境变量 PRINTER_DEVICE_ID；未配置时回退到默认常量 'printer-001'。
# 多台打印机部署时，请为每台分别设置 PRINTER_DEVICE_ID 以区分设备。
PRINTER_DEVICE_ID = os.environ.get("PRINTER_DEVICE_ID", "printer-001")
# 设备归属的租户（软件订阅线）：多租户部署时每台打印机属于某个租户。
# 留空表示默认租户；注册与心跳都会把该值上报给后端，后端据此做租户隔离。
TENANT_ID = os.environ.get("TENANT_ID", "").strip()
# 纸盒容量（张）：优先读环境变量 PRINTER_PAPER_CAPACITY，默认 250。
PRINTER_PAPER_CAPACITY = int(os.environ.get("PRINTER_PAPER_CAPACITY", "250"))
# 心跳上报间隔（秒），每 HEARTBEAT_INTERVAL 秒上报一次。
HEARTBEAT_INTERVAL = 25
# 后台心跳线程的退出开关：程序退出时置位，通知心跳线程停止。
_heartbeat_stop_event = threading.Event()

logging.basicConfig(
    level=logging.INFO,  # 设置日志级别为INFO，会记录INFO及以上级别
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 日志环形缓冲：管理端「实时日志」读的就是这里推上去的内容。
# 只保留最近 300 行，避免长时间运行内存无限增长。
_LOG_BUFFER = deque(maxlen=300)


class _RingLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


_ring_handler = _RingLogHandler()
_ring_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(levelname)s - %(message)s', '%Y-%m-%d %H:%M:%S'))
logging.getLogger().addHandler(_ring_handler)

# 日志 / 指令轮询失败时的限频时间戳，避免服务器不可达时每秒刷一条错误
_last_log_error_at = 0.0
_last_command_error_at = 0.0

def _active_queue():
    """当前设备正在用的打印队列：优先 PRINTER_NAME，其次默认队列，再次第一个。"""
    items = _local_printers()
    for item in items:
        if item.get("in_use"):
            return item
    for item in items:
        if item.get("default"):
            return item
    return items[0] if items else {}


def register_printer():
    """向后台注册本打印机，让后续心跳有对应的设备记录。

    鉴权方式与任务轮询一致：Authorization: Bearer <SECRET_KEY>。
    注册失败只打日志并返回 False，绝不影响打印主流程。
    """
    queue = _active_queue()
    body = {
        "device_id": PRINTER_DEVICE_ID,
        # 展示名：可用环境变量 PRINTER_DISPLAY_NAME 指定（如 "一楼 HP Smart Tank 518"）。
        # 留空时后端保留管理端已改好的名字，不会被设备 ID 覆盖。
        "name": os.environ.get("PRINTER_DISPLAY_NAME", ""),
        # 型号优先用环境变量覆盖，否则取队列识别结果（如 Canon G3800 series）
        "model": os.environ.get("PRINTER_MODEL", "") or queue.get("model", ""),
        "driver": queue.get("driver", ""),
        "conn": queue.get("port", ""),
        "paper_capacity": PRINTER_PAPER_CAPACITY,
        "printers": _local_printers(),
    }
    # 归属租户：传了才上报，避免空值把后端已有的租户归属冲掉
    if TENANT_ID:
        body["tenant_id"] = TENANT_ID
    if queue.get("supports_color") is not None:
        body["supports_color"] = bool(queue["supports_color"])
    if queue.get("supports_duplex") is not None:
        body["supports_duplex"] = bool(queue["supports_duplex"])
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{setting.SERVER_HOST}/api/device/register", data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        res = urllib.request.urlopen(req)
        logging.info("Register printer success: %s", res.read().decode("utf-8"))
        return True
    except Exception as e:
        logging.error("Register printer failed")
        logging.error(e)
        return False


def report_heartbeat():
    """上报一次心跳（status=online），供后端 R2 心跳超时判断离线/断电。

    上报失败只打日志并返回 False，绝不让打印主流程崩溃。
    """
    body = {"device_id": PRINTER_DEVICE_ID, "status": "online",
            "printers": _local_printers()}
    if TENANT_ID:
        body["tenant_id"] = TENANT_ID
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{setting.SERVER_HOST}/api/device/heartbeat", data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        res = urllib.request.urlopen(req)
        logging.info("Heartbeat reported: %s", res.read().decode("utf-8"))
        return True
    except Exception as e:
        logging.error("Heartbeat report failed")
        logging.error(e)
        return False


def heartbeat_loop():
    """后台线程主体：先注册设备，再周期性上报心跳，直到退出开关被置位。"""
    register_printer()
    while not _heartbeat_stop_event.is_set():
        report_heartbeat()
        # 用 wait 代替 sleep，退出开关置位时可立即唤醒线程结束
        _heartbeat_stop_event.wait(HEARTBEAT_INTERVAL)


def start_heartbeat_thread():
    """启动守护心跳线程，不阻塞原有的任务轮询主循环。"""
    thread = threading.Thread(target=heartbeat_loop, name="heartbeat", daemon=True)
    thread.start()
    return thread


def get_new_job():
    try:
        # 带上本机 device_id：后端只返回属于这台机器的订单，避免多台打印机串单
        req = urllib.request.Request(
            f"{setting.SERVER_HOST}/printer/jobs?device_id={quote(PRINTER_DEVICE_ID)}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        res = urllib.request.urlopen(req)

        job_attributes = json.loads(res.read().decode("utf-8")).get("data").get("jobs")
        if job_attributes is None:
            return None
        return job_attributes
    except urllib.request.HTTPError as e:
        logging.error("Network Error: Unable to connect to server")
        logging.error(e)
    except json.decoder.JSONDecodeError as e:
        logging.error("JSON Decode Error: Failed to decode JSON")
        logging.error(e)
    except Exception as e:
        logging.error("Unexpected error occurred")
        logging.error(e)
        logging.error("job attributes: %s", job_attributes)


def report_job_state(folder, filename, state, progress_copies=None,
                     progress_pages=None):
    """向后台回报订单状态：printing / success / failed。

    新版流程：下载后先报 printing（用户端显示「打印中」），
    真正打完再报 success；打印失败报 failed。旧后台收到未知状态时会忽略。
    progress_copies / progress_pages：已提交份数与已提交页数（按份累计）。
    """
    body = {
        "filename": filename,
        "folder": folder,
        "state": state,
        "authentication": setting.SECRET_KEY,
    }
    if progress_copies is not None:
        body["progress_copies"] = progress_copies
    if progress_pages is not None:
        body["progress_pages"] = progress_pages
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{setting.SERVER_HOST}/printer/job/state", data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        urllib.request.urlopen(req)
        return True
    except Exception as e:
        logging.error("Report job state failed: %s (%s)", state, e)
        return False


def push_logs():
    """把最近 300 行日志推给后端，管理端设备详情页可实时查看。

    失败不打断打印主流程，最多每 60 秒记一条 warning。
    """
    global _last_log_error_at
    if not _LOG_BUFFER:
        return False
    body = {"device_id": PRINTER_DEVICE_ID, "lines": list(_LOG_BUFFER)}
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"{setting.SERVER_HOST}/printer/logs", data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        urllib.request.urlopen(req)
        return True
    except Exception as e:
        now = time.time()
        if now - _last_log_error_at > 60:
            _last_log_error_at = now
            logging.warning("Push logs failed: %s", e)
        return False


def get_commands():
    """取走管理端下发的待执行指令（目前只有「重启客户端」）。"""
    req = urllib.request.Request(
        f"{setting.SERVER_HOST}/printer/commands?device_id={quote(PRINTER_DEVICE_ID)}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
    res = urllib.request.urlopen(req)
    return json.loads(res.read().decode("utf-8")).get("data", {}).get("commands") or []


def report_command_result(command_id, ok, result=""):
    """把指令执行结果回报给后端（管理端可看到成功/失败）。"""
    body = {"id": command_id, "ok": ok, "result": result}
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{setting.SERVER_HOST}/printer/command/result", data)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        urllib.request.urlopen(req)
    except Exception as e:
        logging.warning("Report command result failed: %s", e)


def handle_commands():
    """处理管理端下发的指令：restart 会原地重启本客户端进程。"""
    global _last_command_error_at
    try:
        commands = get_commands()
    except Exception as e:
        now = time.time()
        if now - _last_command_error_at > 60:
            _last_command_error_at = now
            logging.warning("Poll commands failed: %s", e)
        return
    for command in commands:
        command_id = command.get("id")
        name = command.get("command")
        if name == "restart":
            logging.warning("收到管理端重启指令（id=%s），客户端即将重启", command_id)
            report_command_result(command_id, True, "客户端正在重启")
            push_logs()
            sleep(0.5)
            # 用 execv 原地替换当前进程，等价于重启；需以 python main.py 方式启动
            os.execv(sys.executable,
                     [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:])
        else:
            logging.warning("收到未知指令：%s", name)
            report_command_result(command_id, False, "unknown command: %s" % name)


def download_job_file(job_attributes):
    folder = job_attributes.get("folder")
    filename = job_attributes.get("filename")
    # 空文件名会让下载路径退化成目录（EISDIR），先在本地拦掉并明确报错，
    # 否则只会看到一句 "Is a directory" 无法定位。
    if not folder or not filename:
        logging.error("任务缺少 folder/filename，无法下载：%s", job_attributes)
        return None
    # 防御：文件名两端的空白与斜杠会让路径退化成目录（EISDIR）
    folder = str(folder).strip().strip("/")
    filename = str(filename).strip().strip("/")
    if not folder or not filename:
        logging.error("任务 folder/filename 非法：%s", job_attributes)
        return None
    try:
        req = urllib.request.Request(
            f"{setting.SERVER_HOST}/printer/file?path={folder}&filename={quote(filename)}"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {setting.SECRET_KEY}")
        res = urllib.request.urlopen(req)

        if not os.path.exists(f"downloads/{folder}"):
            os.mkdir(f"downloads/{folder}")
        with open(f"downloads/{folder}/{filename}", "wb") as f:
            f.write(res.read())
        # 状态改由调用方在上报 printing / success 时处理，避免「下载即完成」
        return f"{folder}/{filename}"
    except urllib.request.HTTPError as e:
        logging.error("Network Error: Unable to connect to server")
        logging.error(e)
    except Exception as e:
        logging.error("Unexpected error occurred")
        logging.error(e)
        logging.error("download failed for folder=%r filename=%r", folder, filename)


def exec_printing_linux(job_attributes, keep_file=False):
    print_copies = job_attributes.get("print_copies")
    print_range_start = job_attributes.get("print_range_start")
    print_range_end = job_attributes.get("print_range_end")
    print_range = str(print_range_start) + "-" + str(print_range_end)
    print_side = job_attributes.get("print_side")
    paper = job_attributes.get("paper") or "A4"
    # 色彩：黑白订单按灰度出纸（IPP 标准属性 print-color-mode）
    color_mode = "color" if job_attributes.get("print_color") == "color" else "monochrome"
    folder = job_attributes.get("folder")
    filename = job_attributes.get("filename")

    if print_side == 'one-sided':
        print_side = 'one-sided'
    elif print_side == 'two-sided-default':
        print_side = 'two-sided-long-edge'
    elif print_side == 'two-sided-long-edge':
        print_side = 'two-sided-long-edge'
    elif print_side == 'two-sided-short-edge':
        print_side = 'two-sided-short-edge'
    try:
        # 本机 CUPS 队列优先用 lp 提交：ipptool 会先压缩成 <file>.gz，
        # 压缩临时文件读不到时整次提交被判失败（纸其实已经出来）。
        # PRINTER_URL 指向 localhost 时直接走 lp，队列名从 URL 末段取。
        job_file = os.path.abspath(f"downloads/{folder}/{filename}")
        queue = None
        if "localhost" in setting.PRINTER_URL or "127.0.0.1" in setting.PRINTER_URL:
            queue = setting.PRINTER_URL.rstrip("/").rsplit("/", 1)[-1]
        if queue:
            exec_print = subprocess.run(
                ["lp", "-d", queue, "-n", str(print_copies),
                 "-o", f"sides={print_side}", "-o", f"page-ranges={print_range}",
                 "-o", f"media={paper}", "-o", f"print-color-mode={color_mode}",
                 job_file],
                capture_output=True,
                text=True,
            )
        else:
            ticket = os.path.abspath("ipp_files/print_ticket_attributes.ipp")
            exec_print = subprocess.run(
                [
                    "ipptool", "-tv", setting.PRINTER_URL, "-f", job_file,
                    "-d", f"sides={print_side}", "-d", f"page-ranges={print_range}",
                    "-d", f"copies={print_copies}", "-d", f"media={paper}",
                    "-d", f"print-color-mode={color_mode}",
                    ticket
                ],
            capture_output=True,
            text=True
            )
        accepted = ("job-id" in exec_print.stdout
                    or "request id is" in exec_print.stdout.lower())
        if exec_print.returncode == 0 and accepted:
            # 逐份打印时前几份要保留文件，最后一份才删除
            if not keep_file and os.path.exists(f"downloads/{folder}/{filename}"):
                os.remove(f"downloads/{folder}/{filename}")
                if len(os.listdir(f"downloads/{folder}")) == 0:
                    os.rmdir(f"downloads/{folder}")
            # 只记订单号文件名：真实文件名不会下发到打印机端
            logging.info("Success print: %s/%s", folder, filename)
            return True
        logging.error("Print Error: Failed to print job: %s/%s", folder, filename)
        logging.error("stdout: %s", exec_print.stdout)
        logging.error("stderr: %s", exec_print.stderr)
        return False
    except Exception as e:
        logging.error("Unexpected error occurred")
        logging.error(e)
        return False

def _find_sumatra():
    """定位 SumatraPDF：优先环境变量/ PATH，其次两个常见安装目录。"""
    if os.path.isfile(SUMATRAPDF_PATH):
        return SUMATRAPDF_PATH
    for candidate in (r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
                      r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe"):
        if os.path.isfile(candidate):
            return candidate
    return SUMATRAPDF_PATH  # 交给 PATH 去查找；找不到会在调用时报错并写日志


def _decode_output(raw):
    """解码子进程输出。

    SumatraPDF / PowerShell 的输出可能是 UTF-8、GBK 或两者混合，
    直接用 text=True 会让 Python 按系统编码(GBK)解码并抛 UnicodeDecodeError，
    把真正的错误信息吞掉。这里按 utf-8 → gbk → 替换非法字符 逐级兜底。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _list_windows_printers():
    """列出本机打印机队列名，打印失败时写进日志方便核对 PRINTER_NAME。"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Printer | Select-Object -ExpandProperty Name"],
            capture_output=True, timeout=30)
        return _decode_output(result.stdout).strip()
    except Exception:
        return ""


# 虚拟打印机/传真不参与发现，避免管理端把「Microsoft Print to PDF」当成真打印机
_VIRTUAL_MARKERS = ("PRINT TO PDF", "XPS DOCUMENT", "ONENOTE", "FAX", "ROOT\\PRINT")
_PRINTER_CACHE = {"at": 0.0, "items": []}


def _is_virtual_printer(name):
    upper = (name or "").upper()
    return any(marker in upper for marker in _VIRTUAL_MARKERS)


_WIN_PRINTER_PS = (
    "$out = Get-CimInstance Win32_Printer | ForEach-Object {"
    "$cfg = $null;"
    "try { $cfg = Get-PrintConfiguration -PrinterName $_.Name -ErrorAction Stop } catch {};"
    "$color = $null; $duplex = $null;"
    "if ($cfg) {"
    "$color = $cfg.Color;"
    "if ($null -ne $cfg.DuplexingMode) { $duplex = [string]$cfg.DuplexingMode }"
    "};"
    "[pscustomobject]@{"
    "Name=$_.Name;DriverName=$_.DriverName;PortName=$_.PortName;"
    "Default=$_.Default;WorkOffline=$_.WorkOffline;"
    "Color=$color;DuplexingMode=$duplex"
    "}}; $out | ConvertTo-Json -Compress"
)


def _collect_printers_windows():
    """枚举本机打印队列，并读 Color / DuplexingMode 判断彩色与自动双面。"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _WIN_PRINTER_PS],
            capture_output=True, timeout=40)
        raw = _decode_output(result.stdout).strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        items = []
        for item in data:
            name = (item.get("Name") or "").strip()
            if not name or _is_virtual_printer(name):
                continue
            color = item.get("Color")
            raw_duplex = item.get("DuplexingMode")
            duplex_mode = "" if raw_duplex is None else str(raw_duplex).lower()
            items.append({
                "name": name,
                "driver": (item.get("DriverName") or "").strip(),
                "port": (item.get("PortName") or "").strip(),
                "default": bool(item.get("Default")),
                "offline": bool(item.get("WorkOffline")),
                "in_use": name == PRINTER_NAME,
                "model": (item.get("DriverName") or "").strip(),
                "supports_color": None if color is None else bool(color),
                # 只有 TwoSided 才是真正的自动双面；Manual（手动翻面）按不支持处理
                "supports_duplex": None if not duplex_mode else duplex_mode.startswith("twosided"),
            })
        return items[:30]
    except Exception as e:
        logging.error("List Windows printers failed: %s", e)
        return []


def _cups_queue_info(name):
    """读 CUPS 队列的型号与能力：型号取 Description，能力取 PPD 选项。

    lpoptions 能正常列出选项时，没有 Duplex 行就说明该队列不支持双面
    （G3800 这类喷墨机就是如此）；命令本身失败才标记为未知（None）。
    """
    info = {"model": "", "supports_color": None, "supports_duplex": None}
    try:
        result = subprocess.run(["lpstat", "-l", "-p", name],
                                capture_output=True, timeout=10)
        for line in _decode_output(result.stdout).splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("description:"):
                info["model"] = stripped.split(":", 1)[1].strip()
                break
    except Exception:
        pass
    try:
        result = subprocess.run(["lpoptions", "-p", name, "-l"],
                                capture_output=True, timeout=10)
        text = _decode_output(result.stdout)
        if text.strip():
            supports_duplex = False
            supports_color = False
            for line in text.splitlines():
                low = line.lower()
                if low.startswith("duplex/"):
                    supports_duplex = ("duplexnotumble" in low
                                       or "duplextumble" in low)
                elif low.startswith("colormodel/") or low.startswith("colormode/"):
                    options = low.split(":", 1)[-1]
                    supports_color = ("rgb" in options) or ("cmyk" in options)
            info["supports_duplex"] = supports_duplex
            info["supports_color"] = supports_color
    except Exception:
        pass
    return info


def _collect_printers_linux():
    """用 lpstat 枚举 CUPS 队列（名称/设备 URI/默认）。"""
    try:
        result = subprocess.run(["lpstat", "-v"], capture_output=True, timeout=15)
        text = _decode_output(result.stdout)
        default = ""
        try:
            d = subprocess.run(["lpstat", "-d"], capture_output=True, timeout=10)
            line = _decode_output(d.stdout).strip()
            if ":" in line:
                default = line.split(":", 1)[1].strip()
        except Exception:
            pass
        items = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("device for "):
                continue
            rest = line[len("device for "):]
            if ":" not in rest:
                continue
            name, uri = rest.split(":", 1)
            name = name.strip()
            if not name or _is_virtual_printer(name):
                continue
            info = _cups_queue_info(name)
            items.append({
                "name": name,
                "driver": "",
                "port": uri.strip(),
                "default": name == default,
                "offline": False,
                "in_use": name == PRINTER_NAME,
                "model": info["model"],
                "supports_color": info["supports_color"],
                "supports_duplex": info["supports_duplex"],
            })
        return items[:30]
    except Exception as e:
        logging.error("List CUPS printers failed: %s", e)
        return []


def _local_printers():
    """本机打印队列（缓存 60 秒，避免每 25 秒心跳都跑一次 PowerShell）。"""
    now = time.time()
    if _PRINTER_CACHE["items"] and now - _PRINTER_CACHE["at"] < 60:
        return _PRINTER_CACHE["items"]
    items = _collect_printers_windows() if IS_WINDOWS else _collect_printers_linux()
    _PRINTER_CACHE["at"] = now
    _PRINTER_CACHE["items"] = items
    return items


def _windows_print_settings(job_attributes):
    """把订单参数转成 SumatraPDF 的 -print-settings。

    例：第 2-5 页、2 份、双面长边 → "2-5,2x,duplexlong"。
    手动双面打印机（如 HP Smart Tank 518）在双面时会弹「翻面后继续」对话框，
    无人值守会卡死，因此可用 PRINTER_FORCE_SIMPLEX=1 强制单面。
    """
    try:
        start = int(job_attributes.get("print_range_start") or 1)
        end = int(job_attributes.get("print_range_end") or start)
        copies = int(job_attributes.get("print_copies") or 1)
    except (TypeError, ValueError):
        # 队列里个别字段可能是字符串，转换失败时退回「第 1 页 1 份」
        start, end, copies = 1, 1, 1
    side = job_attributes.get("print_side") or "one-sided"
    if side == "one-sided" or FORCE_SIMPLEX:
        sides = "simplex"
    elif side == "two-sided-short-edge":
        sides = "duplexshort"
    else:
        sides = "duplexlong"
    pages = str(start) if start == end else f"{start}-{end}"
    paper = (job_attributes.get("paper") or "A4").upper()
    if paper not in ("A4", "A5", "A6", "LETTER"):
        paper = "A4"
    return f"{pages},{copies}x,{sides},paper={paper}"


def exec_printing_windows(job_attributes, keep_file=False):
    """Windows 下用 SumatraPDF 静默打印本机打印机队列。

    支持页范围 / 份数 / 单双面，对应命令行：
        SumatraPDF.exe -print-settings "2-5,2x,simplex" -silent -exit-when-done -print-to "<队列名>" 文件.pdf
    返回是否成功（退出码为 0 视为已交给打印队列）。
    """
    folder = job_attributes.get("folder")
    filename = job_attributes.get("filename")
    file_path = os.path.abspath(f"downloads/{folder}/{filename}")
    # 如实说明：SumatraPDF 没有设置灰度/彩色的参数，Windows 这条链路只能按打印机
    # 驱动的默认色彩输出。要严格灰度，请在打印机驱动里把默认色彩设为「黑白」。
    if (job_attributes.get("print_color") or "bw") == "bw":
        logging.warning("黑白订单：Windows/SumatraPDF 无法强制灰度，将按打印机默认色彩输出")
    settings = _windows_print_settings(job_attributes)
    cmd = [_find_sumatra(), "-print-settings", settings, "-silent", "-exit-when-done"]
    if PRINTER_NAME:
        cmd += ["-print-to", PRINTER_NAME]
    else:
        cmd.append("-print-to-default")
    cmd.append(file_path)
    try:
        # 注意：不加 text=True。Windows 默认按 GBK 解码，SumatraPDF 的输出常是 UTF-8，
        # 强行按文本读取会抛 UnicodeDecodeError 并丢光错误信息。
        result = subprocess.run(cmd, capture_output=True, timeout=180)
        if result.returncode == 0:
            # 逐份打印时前几份要保留文件，最后一份才删除
            if not keep_file and os.path.exists(file_path):
                os.remove(file_path)
                if len(os.listdir(f"downloads/{folder}")) == 0:
                    os.rmdir(f"downloads/{folder}")
            logging.info("Success print (windows): settings=%s printer=%s job=%s",
                         settings, PRINTER_NAME or "(default)", job_attributes)
            return True
        logging.error("Print Error (windows): returncode=%s settings=%s job=%s",
                      result.returncode, settings, job_attributes)
        logging.error("command: %s", cmd)
        logging.error("stdout: %s", _decode_output(result.stdout))
        logging.error("stderr: %s", _decode_output(result.stderr))
        # 0xC0000374 / STATUS_HEAP_CORRUPTION：SumatraPDF 可能已经把任务送进
        # Windows 打印队列，只是在退出时崩溃（实测纸能正常出来）。
        # 这种情况单独提示，别当成"没打出来"。
        if result.returncode in (3221226356, -1073740940):
            logging.warning("SumatraPDF 是在退出时崩溃（0xC0000374 堆损坏），"
                            "打印任务通常已提交到 Windows 队列，请先核对是否出纸；"
                            "建议换用 SumatraPDF 稳定版以消除该错误")
            return False
        # 打印失败时把本机实际存在的打印机队列名列出来，方便核对 PRINTER_NAME
        printers = _list_windows_printers()
        if printers:
            logging.error("本机打印机队列：%s", printers.replace("\n", " | "))
        return False
    except FileNotFoundError:
        logging.error("找不到 SumatraPDF：请安装（便携版即可）并设置环境变量 "
                      "SUMATRAPDF_PATH，或把它加入 PATH")
        return False
    except subprocess.TimeoutExpired:
        logging.error("Print Error (windows): SumatraPDF 超时（180s），任务未确认完成")
        return False
    except Exception as e:
        logging.error("Unexpected error occurred (windows)")
        logging.error(e)
        return False


def exec_printing(job_attributes, keep_file=False):
    """按平台选择打印方式：Windows 用 SumatraPDF，其它（Linux）用 ipptool。"""
    if IS_WINDOWS:
        return exec_printing_windows(job_attributes, keep_file=keep_file)
    return exec_printing_linux(job_attributes, keep_file=keep_file)


def print_job_with_progress(job_attributes):
    """按份逐份提交打印并上报进度；返回是否全部成功。

    - 每份单独提交（print_copies 固定为 1），这样每完成一份就能上报一次进度；
    - 拼单 / 批量整包在服务器侧 print_copies 已是 1，这里只跑一轮，行为不变；
    - 单份失败自动重试 MAX_PRINT_RETRIES 次，仍失败则上报 failed（触发 R6 告警）。
    """
    folder = job_attributes.get("folder")
    filename = job_attributes.get("filename")
    try:
        total_copies = max(int(job_attributes.get("print_copies") or 1), 1)
        start_page = int(job_attributes.get("print_range_start") or 1)
        end_page = int(job_attributes.get("print_range_end") or start_page)
        pages_per_copy = max(end_page - start_page + 1, 0)
    except (TypeError, ValueError):
        total_copies, pages_per_copy = 1, 0

    report_job_state(folder, filename, "printing", 0, 0)
    printed_copies = 0
    print_ok = True
    for copy_index in range(total_copies):
        # 前几份打印后保留文件，最后一份交给打印函数删除
        keep_file = copy_index < total_copies - 1
        one_copy = dict(job_attributes)
        one_copy["print_copies"] = 1
        attempt = 0
        while True:
            # 文件可能在上一次尝试中被删掉或没下载成功，重试前先补齐，
            # 否则 lp/ipptool 只会报 No such file，永远失败。
            if not os.path.exists(f"downloads/{folder}/{filename}"):
                if download_job_file(job_attributes) is None:
                    logging.error("重试前重新下载失败：%s/%s", folder, filename)
            if exec_printing(one_copy, keep_file=keep_file):
                break
            attempt += 1
            if attempt > MAX_PRINT_RETRIES:
                logging.error("打印失败，已重试 %s 次仍失败：%s",
                              MAX_PRINT_RETRIES, filename)
                print_ok = False
                break
            logging.warning("打印失败，%s 秒后第 %s 次重试：%s",
                            PRINT_RETRY_DELAY_SECONDS, attempt, filename)
            sleep(PRINT_RETRY_DELAY_SECONDS)
        if not print_ok:
            break
        printed_copies = copy_index + 1
        report_job_state(folder, filename, "printing", printed_copies,
                         printed_copies * pages_per_copy)

    if print_ok:
        report_job_state(folder, filename, "success", total_copies,
                         total_copies * pages_per_copy)
    else:
        report_job_state(folder, filename, "failed", printed_copies,
                         printed_copies * pages_per_copy)
    return print_ok


if __name__ == "__main__":
    if not os.path.exists('downloads'):
        os.makedirs('downloads')
    # 启动后台心跳线程（守护线程，独立于下面的任务轮询主循环）
    heartbeat_thread = start_heartbeat_thread()
    last_log_push = 0.0
    # 下载失败计数：连续失败 3 次就上报 failed，避免坏文件把整条队列堵死
    download_failures = {}
    try:
        while True:
            sleep(1)
            # 每秒轮询一次管理端指令（重启客户端）
            handle_commands()
            # 每 10 秒把最近日志推给后端，管理端设备详情页可看到实时日志
            if time.time() - last_log_push >= 10:
                last_log_push = time.time()
                push_logs()
            job_attributes = get_new_job()
            if job_attributes is None:
                continue

            folder = job_attributes.get("folder")
            filename = job_attributes.get("filename")
            file_path = download_job_file(job_attributes)
            if file_path is None:
                key = (folder, filename)
                failures = download_failures.get(key, 0) + 1
                if failures >= 3:
                    logging.error("下载连续失败 %s 次，标记订单失败：%s", failures, filename)
                    report_job_state(folder, filename, "failed")
                    download_failures.pop(key, None)
                else:
                    download_failures[key] = failures
                continue
            download_failures.pop((folder, filename), None)

            # 按份逐份提交并上报进度；失败自动重试，最终失败会触发 R6 告警
            print_job_with_progress(job_attributes)
    finally:
        # 程序退出时通知心跳线程停止
        _heartbeat_stop_event.set()
