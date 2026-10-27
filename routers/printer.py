import os
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.responses import FileResponse

from jwt import auth_device_id, security, secret_key_matches
from models import ReturnResult, UpdateJob, UpdateState
from print_queue import print_queue_singleton
import db

router = APIRouter()

# 内存打印队列是普通 list，认领动作必须在同一把锁里完成，
# 否则两台设备并发取任务会同时拿到同一个「未指定设备」的订单。
_QUEUE_LOCK = threading.Lock()

def verify_secret_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """打印机端鉴权：商家密钥（老部署）或零配置配发的设备令牌都放行。

    返回 merchant 或 device:<device_id>，具体接口再校验设备归属。
    """
    if secret_key_matches(credentials.credentials):
        return "merchant"
    device_id = db.device_id_by_token(credentials.credentials)
    if device_id:
        return "device:" + device_id
    raise HTTPException(
        status_code=401,
        detail="Invalid secret key",
    )

# 打印机端只下发打印必需的字段：
# 真实文件名、金额、批量名单、取件码都不出服务端（取件码只在用户端订单详情展示）。
DEVICE_JOB_FIELDS = ("folder", "filename", "print_copies", "print_range_start",
                     "print_range_end", "print_side", "paper", "print_color",
                     "printer_id")


def _device_job(job):
    return {key: job.get(key) for key in DEVICE_JOB_FIELDS}


@router.get("/printer/jobs")
def printer_get_job(device_id: str = "", _auth=Depends(verify_secret_key)):
    """打印机端取任务。

    device_id 用于「按设备隔离」：每台机器只取自己的订单，绝不串单。
    未指定设备的订单（用户下单时没选机器）由第一台来取任务的设备认领，
    认领后其他设备就取不到了，避免两台机器各打一份。

    旧版客户端不带 device_id，仍按队首返回（无法设备隔离），
    升级到新版 client 后自动走设备隔离逻辑。
    """
    print_queue = print_queue_singleton().data
    scope_device = auth_device_id(_auth)
    if scope_device:
        # 设备令牌只能取自己的任务；传了别人的 device_id 一律当没任务
        if device_id and device_id != scope_device:
            return ReturnResult(200, 'success', {'jobs': None})
        device_id = scope_device
    if len(print_queue) == 0:
        return ReturnResult(200, 'success', {'jobs': None})
    # device_id 必须已注册：否则任意字符串都能把「未指定设备」的订单冒领走
    device = db.get_device(device_id) if device_id else None
    if device_id and device is None:
        return ReturnResult(200, 'success', {'jobs': None})
    if not device_id:
        return ReturnResult(200, 'success', {'jobs': _device_job(print_queue[0])})

    tenant_id = (device or {}).get("tenant_id") or "default"
    # 认领与读取必须在锁内完成：两台设备并发时不能让同一单被返回两次
    with _QUEUE_LOCK:
        for job in print_queue:
            owner = job.get("printer_id")
            if owner == device_id:
                return ReturnResult(200, 'success', {'jobs': _device_job(job)})
            if owner:
                continue
            # 未指定设备的订单只允许同租户设备认领，避免跨租户串单
            if scope_device:
                row = db.get_job(job.get("folder"), job.get("filename"))
                job_tenant = (row or {}).get("tenant_id") or "default"
                if job_tenant != tenant_id:
                    continue
            # 以数据库条件更新为准：只有真正抢到（rowcount=1）才返回任务
            if db.assign_job_printer(job.get("folder"), job.get("filename"),
                                     device_id) <= 0:
                continue
            job["printer_id"] = device_id
            return ReturnResult(200, 'success', {'jobs': _device_job(job)})
    return ReturnResult(200, 'success', {'jobs': None})


@router.get("/printer/file")
def get_file(path: str, filename: str, _auth=Depends(verify_secret_key)):
    # 只允许 pending_files 下的单层目录/文件，挡住 ../ 路径穿越
    safe_path = os.path.basename((path or "").replace("\\", "/"))
    safe_name = os.path.basename((filename or "").replace("\\", "/"))
    root = os.path.abspath("pending_files")
    full = os.path.abspath(os.path.join(root, safe_path, safe_name))
    if not safe_path or not safe_name or not full.startswith(root + os.sep):
        raise HTTPException(status_code=404, detail="file not found")
    scope_device = auth_device_id(_auth)
    if scope_device:
        # 设备令牌只能下载自己名下（或尚未认领）的任务文件
        job = db.get_job(safe_path, safe_name)
        if job is None or (job.get("printer_id")
                           and job["printer_id"] != scope_device):
            raise HTTPException(status_code=404, detail="file not found")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(full)


class CommandResult(BaseModel):
    """客户端回报指令执行结果。"""

    id: int
    ok: bool = True
    result: str = ""


class DeviceLogsPush(BaseModel):
    """客户端推送的最近日志行。"""

    device_id: str
    lines: list[str] = []


@router.get("/printer/commands")
def printer_commands(device_id: str, _auth=Depends(verify_secret_key)):
    """打印机端轮询待执行指令（目前只有「重启客户端」）。

    服务端取走即标记为 sent，过期（10 分钟）未取走的指令自动作废。
    """
    scope_device = auth_device_id(_auth)
    if scope_device and scope_device != device_id:
        return ReturnResult(200, "success", {"commands": []})
    return ReturnResult(200, "success", {"commands": db.take_pending_commands(device_id)})


@router.post("/printer/command/result")
def printer_command_result(payload: CommandResult, _auth=Depends(verify_secret_key)):
    """打印机端回报指令执行结果。"""
    scope_device = auth_device_id(_auth)
    if scope_device:
        command = db.get_command(payload.id)
        # 设备令牌只能回报自己的指令，防止改写别台设备的执行结果或吞掉指令
        if command is None or command.get("device_id") != scope_device:
            raise HTTPException(status_code=403, detail="command belongs to another device")
    db.finish_command(payload.id, payload.ok, payload.result)
    return ReturnResult(200, "success", {})


@router.post("/printer/logs")
def printer_push_logs(payload: DeviceLogsPush, _auth=Depends(verify_secret_key)):
    """打印机端推送最近日志（管理端设备详情页实时查看）。"""
    scope_device = auth_device_id(_auth)
    if scope_device and scope_device != payload.device_id:
        raise HTTPException(status_code=403, detail="device mismatch")
    saved = db.set_device_logs(payload.device_id, payload.lines)
    return ReturnResult(200, "success", {"saved": saved})


def _remove_from_queue(folder, filename):
    """把任务从内存队列里移除（可能有多条同名的重复项），返回是否移除了。

    原地修改列表（queue[:] = kept），保持单例引用不变，
    其他模块持有的 print_queue 引用仍然有效。
    """
    queue = print_queue_singleton().data
    kept = [item for item in queue
            if not (item.get("folder") == folder
                    and item.get("filename") == filename)]
    removed = len(queue) - len(kept)
    if removed:
        queue[:] = kept
    return removed > 0


@router.post("/printer/job/state")
def update_job_status(update_state: UpdateState, _auth=Depends(verify_secret_key)):
    # header 已由 verify_secret_key 校验；body 里的 authentication 仅保留字段兼容，
    # 不再重复比较（双检不增加安全性，还容易让调用方误以为 body 才是鉴权依据）。
    folder = update_state.folder
    filename = update_state.filename
    job = db.get_job(folder, filename)
    if job is None:
        return ReturnResult(200, "job not found", {})
    scope_device = auth_device_id(_auth)
    # 设备令牌只能上报自己名下的订单；printer_id 为空说明尚未被任何设备认领，
    # 同样拒绝，避免 A 设备改掉 B 租户待打印订单的状态
    if scope_device and job.get("printer_id") != scope_device:
        raise HTTPException(status_code=403, detail="job belongs to another device")

    state = (update_state.state or "success").lower()

    # 只接受客户端真实上报的状态；未知状态直接拒绝，
    # 否则会一路落到下面的「完成」分支，把订单误标完成并累计用纸。
    if state not in ("printing", "downloaded", "failed", "success",
                     "completed", "done"):
        return ReturnResult(200, "unknown state", {})

    # 打印进度：客户端按「已提交份数 / 已打印页数」上报，写入订单供用户端与管理端展示
    if (update_state.progress_copies is not None
            or update_state.progress_pages is not None):
        db.set_job_progress(folder, filename,
                            update_state.progress_copies,
                            update_state.progress_pages)

    # 打印中：出队但不算完成，也不累计用纸/不删文件。
    # 新版客户端下载后先报 printing，真正打完再报 success；
    # 旧版客户端下载后直接报 success，仍按原逻辑即完成。
    if state in ("printing", "downloaded"):
        db.set_job_status(folder, filename, "printing")
        _remove_from_queue(folder, filename)
        return ReturnResult(200, "success", {"state": "printing"})

    # 打印失败：标记 failed，保留文件便于人工排查
    if state == "failed":
        db.set_job_status(folder, filename, "failed")
        _remove_from_queue(folder, filename)
        return ReturnResult(200, "success", {"state": "failed"})

    # 完成：按「实际耗纸张数」累计到设备，并重算 paper_left。
    # 用带 status!='completed' 条件的 UPDATE 原子地「认领」完成动作：
    # 只有第一次完成（rowcount>0）才累计用纸 / 判定免单，客户端并发或重复上报
    # 都无法重复累计（先读后写判断在多进程/多线程下会串行失败）。
    guarantee = None
    if db.complete_job(folder=folder, filename=filename):
        start = job.get('print_range_start')
        end = job.get('print_range_end')
        copies = job.get('print_copies')
        start = start if start is not None else 1
        end = end if end is not None else 1
        copies = copies if copies is not None else 1
        pages_in_range = max(end - start + 1, 0)
        if job.get('print_side') == 'one-sided':
            sheets_per_copy = pages_in_range
        else:
            # 双面（default / long-edge / short-edge）：每张纸正反两面
            sheets_per_copy = (pages_in_range + 1) // 2
        sheets = sheets_per_copy * copies
        printer_id = job.get('printer_id')
        if printer_id:
            db.add_sheets_used(printer_id, sheets)
        # 交付承诺：完成时刻超过承诺完成时间即免单（同步收款流水为免收/待退款）
        guarantee = db.apply_guarantee(folder, filename)

    _remove_from_queue(folder, filename)
    # 清理服务端待打印文件（文件名入队时已统一成 .pdf）
    pdf_path = f'pending_files/{folder}/{filename}'
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    folder_path = f'pending_files/{folder}/'
    if os.path.isdir(folder_path) and not os.listdir(folder_path):
        os.rmdir(folder_path)
    if guarantee and guarantee.get("waived"):
        return ReturnResult(200, "success", {
            "state": "completed",
            "waived": True,
            "reason": guarantee.get("reason"),
        })
    # 重复上报（订单此前已完成）：不再按当前时间重新判定是否超时，
    # 直接回放库里已记录的免单结果，让客户端重试也能看到同样的结论。
    if job.get("waived"):
        return ReturnResult(200, "success", {
            "state": "completed",
            "waived": True,
            "reason": job.get("waive_reason"),
        })
    return ReturnResult(200, "success", {})