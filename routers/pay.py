import os
import json
import shutil
import time
from datetime import datetime, timedelta
import secrets
from typing import Optional

from dinero import Dinero
from dinero.currencies import CNY
from fastapi import APIRouter, Form, Header, Depends
from starlette.responses import HTMLResponse
from typing_extensions import Annotated
from jose.jwt import encode
import logging
import traceback
from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient
from alipay.aop.api.domain.AlipayTradeCreateModel import AlipayTradeCreateModel
from alipay.aop.api.response.AlipayTradeCreateResponse import AlipayTradeCreateResponse
from alipay.aop.api.request.AlipayTradePrecreateRequest import (
    AlipayTradePrecreateRequest,
)

import jwt
import jose
import batch_pdf
import stamp_pdf
import db
import delivery
import library
import pricing
import print_queue
from pydantic import BaseModel
from global_vars.files_attributes_singleton import files_attributes_singleton
from setting import SERVER_HOST, APP_ID, ALIPAY_PRIVATE_KEY, ALIPAY_PUBLIC_KEY, SECRET_KEY, ALGORITHM
from models import FileList, ReturnResult

router = APIRouter()


class WechatPaySubmit(BaseModel):
    """个人微信收款码付款后，用户自己点「我已支付」提交的请求体。

    没有商户号，后端拿不到微信的到账回调，因此这里只能记为「用户声明已付」，
    由管理端在账单流水页人工核对到账后再确认收款（不假装已自动收款）。
    """

    out_trade_no: str
    payer_note: Optional[str] = None


def _job_filename(out_trade_no, index, total):
    """打印机端只能看到订单号：单文件用订单号，多文件加序号。"""
    base = (out_trade_no or "JOB").replace("/", "_").replace("\\", "_")
    return f"{base}.pdf" if total <= 1 else f"{base}_{index + 1}.pdf"


# 定时打印的限制：最少提前 1 分钟（太近等于即打，没有意义），最多预约 7 天。
SCHEDULE_MIN_LEAD_SECONDS = 60
SCHEDULE_MAX_DAYS = 7


def _parse_scheduled_at(raw):
    """解析「预约出纸时间」，返回 (入库文本, datetime, 错误提示)。

    接受 unix 秒（数字或数字字符串）以及常见的日期时间字符串；
    留空表示立即打印，返回 (None, None, None)。
    """
    if raw is None or str(raw).strip() == "":
        return None, None, None
    text = str(raw).strip()
    value = None
    try:
        value = datetime.fromtimestamp(float(text))
    except (TypeError, ValueError):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                value = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if value is None:
        return None, None, "预约时间格式不正确，请重新选择"
    lead = (value - datetime.now()).total_seconds()
    if lead < SCHEDULE_MIN_LEAD_SECONDS:
        return None, None, "预约时间至少要晚于当前时间 1 分钟"
    if lead > SCHEDULE_MAX_DAYS * 86400:
        return None, None, f"最多只能预约 {SCHEDULE_MAX_DAYS} 天内的时间"
    return value.strftime("%Y-%m-%d %H:%M:%S"), value, None


def _cache_reprint_source(folder, source_path, filename):
    """入队前把源文件另存一份到 reprint_cache，供「格式不对免费重印」使用。

    入队时 uploads 下的源文件会被搬走/删除，若不另存，重印审批永远找不到文件。
    副本由后台任务在订单完成 5 分钟后清理，与交付承诺的隐私口径一致。
    失败不阻塞下单（重印时如实提示重新上传）。
    """
    try:
        os.makedirs(f'reprint_cache/{folder}', exist_ok=True)
        cache_path = f'reprint_cache/{folder}/{filename}'
        if os.path.abspath(source_path) != os.path.abspath(cache_path):
            shutil.copyfile(source_path, cache_path)
    except OSError:
        logging.exception("reprint cache copy failed, reprint will need re-upload")


def _bill_eta(bill_attributes):
    """取账单里的交付承诺 (预计耗时, 承诺完成时间)；缺失时按账单页数补算。"""
    seconds = bill_attributes.get('eta_seconds')
    deadline = bill_attributes.get('eta_deadline')
    if seconds and deadline:
        return (seconds, deadline)
    return delivery.estimate_eta_seconds(
        bill_attributes.get('printer_id'), bill_attributes.get('pages') or 0)


def _enqueue_paid_order(files_attributes, out_trade_no=None, eta=None,
                        share_library=False, library_category="其他"):
    """把已支付的订单文件从 uploads 搬到 pending_files 并入队（原 /pay/return 逻辑）。

    抽成公共函数，供支付宝回调与个人微信收款提交两条路径复用，避免两处逻辑走偏。
    out_trade_no 写入每一条订单，管理端可按支付单号对账。
    eta 为 (预计耗时秒数, 承诺完成时间)，交付承诺的免单判定基线，注入每条订单。

    隐私：
    - 待打印文件名改成订单号，打印机端看不到真实文件名；
    - 取件码不打印在纸上，只在用户端订单详情展示，避免多耗墨粉；
    - 班级批量：把「姓名封皮 + 正文」拼成一个 PDF（每份一叠），
      文件本身已经包含 N 份，所以客户端按 1 份、1..总页数 打印即可。

    定时打印：带 scheduled_at 的订单同样在这里把文件备好并落库，
    但不进内存队列，状态记为 scheduled，到点由 release_scheduled_jobs 放行。
    """
    total = len(files_attributes)
    for index, file_attributes in enumerate(files_attributes):
        folder = file_attributes.get('folder')
        converted_filename = file_attributes.get('filename').rsplit(".", 1)[0] + '.pdf'
        os.makedirs(f'pending_files/{folder}', exist_ok=True)
        # 重印审批会显式指定 source_path（重印副本）；普通下单按会话目录取源文件
        source_path = (file_attributes.get('source_path')
                       or f'uploads/{folder}/converted/{converted_filename}')
        # 打印机端只拿到订单号文件名，真实文件名留在数据库 display_name
        token = _job_filename(out_trade_no, index, total)
        pending_path = f"pending_files/{folder}/{token}"

        batch = file_attributes.get('batch') or {}
        copies = int(file_attributes.get('print_copies') or 1)
        start = int(file_attributes.get('print_range_start') or 1)
        end = int(file_attributes.get('print_range_end') or 1)
        skip_pages = file_attributes.get('skip_blank_pages') or []
        ready_pages = None

        # 交付承诺「格式不对免费重印」：入队会把 uploads 下的源文件搬走或删除，
        # 这里先另存一份重印副本；订单完成 5 分钟后由后台任务清理。
        _cache_reprint_source(folder, source_path, converted_filename)

        # 拼单：每个人一份自己的文件，也要加姓名封皮（便于分拣）；
        # 班级批量：同一个文件印多份，每份一张姓名封皮。
        # 两种都靠 batch_pdf 拼成「封皮 + 正文」的整包文件。
        if batch and (copies > 1 or batch.get('mode') == 'group'):
            try:
                ready_pages, content_pages = batch_pdf.build(
                    source_path, pending_path,
                    names=batch.get('names') or [],
                    copies=copies,
                    start=start,
                    end=end,
                    order_no=out_trade_no or "",
                    owner=batch.get('owner') or "",
                    filename=converted_filename,
                    duplex=(file_attributes.get('print_side')
                            or 'one-sided') != 'one-sided',
                    skip_pages=skip_pages,
                    mode=batch.get('mode') or 'unified',
                )
                batch['content_pages'] = content_pages
                batch['copies'] = copies
                file_attributes['print_copies'] = 1
                file_attributes['print_range_start'] = 1
                file_attributes['print_range_end'] = ready_pages
            except Exception:
                # 拼封皮失败不能让用户白付钱：退回普通打印（没有姓名封皮）
                logging.exception("batch cover build failed, fallback to plain print")
                ready_pages = None
                if os.path.exists(pending_path):
                    os.remove(pending_path)
        else:
            try:
                # 只复制用户选择的页范围（取件码不印在纸上）
                ready_pages = stamp_pdf.build(
                    source_path, pending_path, start, end,
                    skip_pages=skip_pages)
                file_attributes['print_range_start'] = 1
                file_attributes['print_range_end'] = ready_pages
            except Exception:
                logging.exception("stamp pdf failed, fallback to plain print")
                ready_pages = None
                if os.path.exists(pending_path):
                    os.remove(pending_path)

        # 重印副本不能像普通源文件那样被搬走/删掉：留着它，
        # 万一重印件本身又打坏，用户还能再次申请（后台按完成 5 分钟统一清理）。
        from_cache = source_path.replace("\\", "/").startswith("reprint_cache/")
        if ready_pages is None:
            if from_cache:
                if os.path.exists(pending_path):
                    os.remove(pending_path)
                shutil.copyfile(source_path, pending_path)
            else:
                os.replace(source_path, pending_path)
        elif os.path.exists(source_path) and not from_cache:
            os.remove(source_path)

        # 入队/入库统一用订单号文件名；真实文件名只用于服务端展示
        file_attributes['filename'] = token
        file_attributes['display_name'] = (file_attributes.get('display_name')
                                           or converted_filename)
        if out_trade_no:
            file_attributes['out_trade_no'] = out_trade_no
        # 交付承诺：预计耗时与承诺完成时间随订单入库，超时免单按此判定
        if eta:
            file_attributes['eta_seconds'] = eta[0]
            file_attributes['eta_deadline'] = eta[1]
        # 共享资料库：只复制「本次实际打印范围」的成品 PDF，不动用户源文件。
        # agent 初审在后台线程里跑，不阻塞付款与入队。
        if share_library and os.path.isfile(pending_path):
            try:
                library.capture_pending(
                    pending_path,
                    title=(file_attributes.get('display_name')
                           or converted_filename),
                    category=library_category,
                    uploader_uid=file_attributes.get('user_id'),
                    tenant_id=file_attributes.get('tenant_id') or 'default',
                    order_no=out_trade_no or '',
                    index=index,
                )
            except Exception:
                logging.exception("library capture failed")
        print_queue_global = print_queue.print_queue_singleton()
        if file_attributes.get('scheduled_at'):
            # 定时打印：文件已备好、订单先落库，但不进队列；
            # 到点由后台 release_scheduled_jobs 改成 pending 并放行
            file_attributes['status'] = 'scheduled'
        else:
            print_queue_global.data.append(file_attributes)
        # 同时写入数据库，保证重启不丢订单；
        # 显式把 printer_id 传给 add_job，打通订单与具体设备的关联
        db.add_job(file_attributes, printer_id=file_attributes.get('printer_id'))
        # 资料库打印量统计：付费入队时累计（下载/取用不算），用于上传者的贡献折扣
        library_doc_id = file_attributes.get('library_doc_id')
        if library_doc_id:
            try:
                start = int(file_attributes.get('print_range_start') or 1)
                end = int(file_attributes.get('print_range_end') or 1)
                copies = max(int(file_attributes.get('print_copies') or 1), 1)
                db.add_library_print(int(library_doc_id),
                                     max(end - start + 1, 0) * copies)
            except Exception:
                logging.exception("library print count failed")


def _files_summary(files_attributes):
    """把订单里的文件清单压成一行摘要，写入收款流水方便管理端对账。

    例：「a.pdf 1-5页 单面 x2；b.pdf 2-3页 双面 x1」。
    """
    side_label = {'one-sided': '单面'}
    color_label = {'bw': '黑白', 'color': '彩色'}
    parts = []
    for item in files_attributes or []:
        filename = item.get('filename') or '未命名'
        start = item.get('print_range_start')
        end = item.get('print_range_end')
        side = side_label.get(item.get('print_side'), '双面')
        color = color_label.get(item.get('print_color'), '黑白')
        copies = item.get('print_copies') or 1
        paper = item.get('paper') or 'A4'
        parts.append(f"{filename} {start}-{end}页 {color} {side} {paper} x{copies}")
        batch = item.get('batch')
        if batch:
            parts.append(f"统一取件（名单{len(batch.get('names') or [])}人备注）")
    return "；".join(parts)


@router.post("/pay/bill/create")
async def create_bill(request_body: FileList, payload: dict = Depends(jwt.verify_token)):
    directory = payload.get("token")
    expire = payload.get("exp")
    # 幂等：同一会话 15 秒内重复下单（典型是连点两次「立即支付」）直接返回同一张账单，
    # 避免生成两张都能付款的账单、导致重复收费与重复打印。
    for _no, _bill in db.list_bills():
        if (_bill.get("directory") == directory
                and time.time() - (_bill.get("created_ts") or 0) < 15
                # 共享资料库会改变价格：勾选状态不同就不复用旧账单
                and bool(_bill.get("share_library")) == bool(
                    getattr(request_body, "share_library", False))):
            _cfg = db.get_pay_config()
            return ReturnResult(200, "wechat payment required", {
                'out_trade_no': _no,
                'total_price': _bill.get('total_price'),
                'pickup_code': _bill.get('pickup_code'),
                'saved': _bill.get('saved'),
                'eta_seconds': _bill.get('eta_seconds'),
                'eta_deadline': _bill.get('eta_deadline'),
                'scheduled_at': _bill.get('scheduled_at'),
                'share_library': bool(_bill.get('share_library')),
                'library_discount': _bill.get('library_discount', 0),
                'library_discount_rate': _bill.get('library_discount_rate', 0),
                'library_print_count': _bill.get('library_print_count', 0),
                'method': 'wechat_personal',
                'wechat_qr': _cfg.get('wechat_qr'),
                'payee': _cfg.get('payee'),
                'url': None,
            })
    request_body_attributes = request_body.fileList
    # 文件所属会话目录以服务端 token 为准（前端也会带 folder，这里强制覆盖，
    # 避免入队/删除时被前端传参带偏，出现 uploads/None 这类错误路径）
    uid = payload.get("uid")
    for file_attributes in request_body_attributes:
        file_attributes["folder"] = directory
        # 登录账号：订单按 user_id 跨设备归档
        if uid:
            file_attributes["user_id"] = int(uid)
    # 用户下单时选择的打印机 device_id（可选）。
    # 为空时保持旧行为：不注入 printer_id，流程照常跑通。
    printer_id = getattr(request_body, "printer_id", None)
    if printer_id:
        # 设备必须真实存在：否则订单入队后没有设备会来取，用户付了钱也打不出来
        device = db.get_device(printer_id)
        if device is None:
            return ReturnResult(200, '所选打印机不存在，请重新选择', {})
        # 打印机不支持自动双面（如佳能 G3800）时拦下双面单，
        # 避免用户按双面价（0.5/张）付钱却只出单面
        if device.get("supports_duplex") is False:
            duplex_files = [f for f in request_body_attributes
                            if (f.get("print_side") or "one-sided") != "one-sided"]
            if duplex_files:
                return ReturnResult(
                    200, '所选打印机不支持自动双面，请改为单面或换一台打印机', {})
        # 把 printer_id 注入每个文件属性，
        # 之后会随订单一起进入打印队列（print_queue）并写入数据库（print_jobs.printer_id）
        for file_attributes in request_body_attributes:
            file_attributes["printer_id"] = printer_id

    # SaaS 订阅生效：目标点位停用 / 订阅到期时不再接单，避免收了钱打不出来。
    order_tenant = ((device.get("tenant_id") if printer_id else None) or "default")
    tenant_state = db.tenant_service_state(order_tenant)
    if not tenant_state["active"]:
        return ReturnResult(
            200, f"该点位服务暂停（{tenant_state['reason']}），暂不能下单", {})

    # 定时打印（预约出纸，可选）：留空时行为与原来完全一致。
    # 付费后先不入队，到点才由后台放行，避免「早八点要的讲义」现在就被打出来。
    scheduled_text, scheduled_dt, schedule_error = _parse_scheduled_at(
        getattr(request_body, "scheduled_at", None))
    if schedule_error:
        return ReturnResult(200, schedule_error, {})
    if scheduled_text:
        for file_attributes in request_body_attributes:
            file_attributes["scheduled_at"] = scheduled_text

    # 班级/社团批量：一次只能一个文件；一个人付全款、统一取件。
    # 名单只作备注（不绑定份数、不参与计价、不做逐人分账）。
    batch = getattr(request_body, "batch", None)
    if batch:
        if len(request_body_attributes) != 1:
            return ReturnResult(200, "班级批量一次只能包含一个文件", {})
        names = []
        seen = set()
        for raw in batch.get("names") or []:
            name = str(raw).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        if len(names) > 100:
            return ReturnResult(200, "批量名单最多 100 人", {})
        copies = int(request_body_attributes[0].get("print_copies") or 1)
        if copies < 1 or copies > 100:
            return ReturnResult(200, "批量份数需在 1-100 之间", {})
        # 封皮 + 全部份数要在服务端拼成一个 PDF，太大会拖慢下单/吃内存
        try:
            pages_per_copy = (int(request_body_attributes[0].get("print_range_end") or 1)
                              - int(request_body_attributes[0].get("print_range_start") or 1) + 1)
        except (TypeError, ValueError):
            pages_per_copy = 1
        if copies * max(pages_per_copy, 1) > 2000:
            return ReturnResult(200, "批量总页数超过 2000 页，请拆成多单", {})
        # 份数少于名单时，只给前 N 个人印封皮，避免出现没有正文的封皮
        names = names[:copies]
        batch = {"mode": "unified", "names": names,
                 "owner": (batch.get("owner") or "").strip()}
        for file_attributes in request_body_attributes:
            file_attributes["batch"] = batch

    # 共享资料库（可选）：用户自愿公开本次打印成品，审核通过后才可见。
    # 勾选后本单 95 折；班级批量含姓名封皮，属于他人信息，禁止共享。
    share_library = bool(getattr(request_body, "share_library", False))
    library_category = library.clean_category(
        getattr(request_body, "library_category", None))
    if share_library and batch:
        return ReturnResult(200, "班级批量包含姓名封皮，不支持共享到资料库", {})
    # 资料库优惠 = max(本单共享 5%, 该用户历史共享资料的贡献折扣)。
    # 贡献折扣按「已公开资料被真实打印的次数」分档，打印量越多折扣越高。
    contribution = library.contribution_of(uid)
    contribution_rate = float(contribution.get("contribution_rate") or 0)
    library_rate = max(library.DISCOUNT_RATE if share_library else 0.0,
                       contribution_rate)
    files_attributes_global = files_attributes_singleton()
    # 会话目录可能已过期/被清理，取不到时按空列表处理，
    # 后续的「订单生成错误」分支会正常返回，而不是对 None 迭代导致 500
    files_attributes = files_attributes_global.data.get(directory) or []
    price = Dinero(0, CNY)
    # 单价按「这台机器」的实际生效价取：该机有独立定价就用它的，否则跟随全局。
    # 用户端选机后同样拉这台机器的价，保证「前端显示价 = 后端结算价」。
    price_config = db.effective_price(printer_id)
    # 账单附带计费页数与预计耗纸张数，写入收款流水供管理端对账
    bill_pages = 0
    bill_sheets = 0
    bill_saved = 0.0
    library_saved = 0.0

    for request_file in request_body_attributes:
        print_filename = request_file.get("filename")
        print_side = request_file.get('print_side')
        # 资料库来源校验：只允许真实存在且已公开的资料，避免伪造 id 刷打印量
        library_doc_id = request_file.get("library_doc_id")
        if library_doc_id is not None:
            try:
                lib_doc = db.get_library_doc(int(library_doc_id))
            except (TypeError, ValueError):
                lib_doc = None
            if not lib_doc or lib_doc.get("status") != "published":
                request_file.pop("library_doc_id", None)
        try:
            print_copies = int(request_file.get('print_copies'))
            print_range_start = int(request_file.get('print_range_start'))
            print_range_end = int(request_file.get('print_range_end'))
        except Exception as e:
            return ReturnResult(200, '订单不合法', {})

        matched_file = None
        for file_attributes in files_attributes:
            if print_filename == file_attributes.filename:
                matched_file = file_attributes
                break
        # 只能对本会话里真实存在的文件下单：匹配不到直接拒绝，
        # 否则会按不存在的页码范围计价，搬文件时才抛 500。
        if matched_file is None:
            return ReturnResult(200, '文件不存在或已过期，请重新上传', {})
        # total_pages 可能缺失（例如图片转换异常），缺失时跳过上限校验，
        # 不能因为属性不存在就抛 500。
        total_pages = getattr(matched_file, "total_pages", None)

        # 页码下界必须 >= 1：允许 0/负数会让计费页数被放大（end-start+1），
        # 既会虚增金额，也会让空白页检测对超大区间做长时间循环。
        if print_range_start < 1 or print_range_end < print_range_start:
            return ReturnResult(200, '订单打印范围不合法', {})
        # 上界必须有确切页数：total_pages 为 None 时若不拦，range(start, end+1) 会被
        # 巨大 end 拖死（pricing 的 chargeable_count 是逐页循环）。
        if total_pages is None:
            return ReturnResult(200, '文件还没转换完成，请稍后再下单', {})
        if total_pages < print_range_end:
            return ReturnResult(200, '订单打印范围不合法', {})
        if print_copies < 1 or print_copies > 100:
            return ReturnResult(200, '订单打印份数需在 1-100 之间', {})
        if print_side == 'one-sided' or print_side == 'two-sided-default' or print_side == 'two-sided-short-edge' or print_side == 'two-sided-long-edge':
            pass
        else:
            return ReturnResult(200, '订单打印份打印方向不合法', {})

        # 纸张规格：目前支持 A4/A5/A6/LETTER，默认 A4。
        # 打印机端会把它转成 media=… 传给 CUPS / SumatraPDF。
        paper = (request_file.get("paper") or "A4").strip().upper()
        if paper not in ("A4", "A5", "A6", "LETTER"):
            return ReturnResult(200, '纸张规格不合法', {})
        request_file["paper"] = paper

        # 色彩 + 单双面计价：
        #   单面 黑白 0.3 元/页、彩色 0.8 元/页；
        #   双面 黑白 0.5 元/张、彩色 1.5 元/张（一张纸两面）；
        #   双面奇数页时，剩下的一页按单面价计。
        print_color = (request_file.get("print_color") or "bw").strip().lower()
        if print_color not in ("bw", "color"):
            return ReturnResult(200, '色彩模式不合法', {})
        request_file["print_color"] = print_color
        pdf_path = None
        if matched_file is not None:
            pdf_name = matched_file.filename.rsplit(".", 1)[0] + ".pdf"
            pdf_path = f"uploads/{matched_file.folder}/converted/{pdf_name}"

        # 计价统一交给 pricing.price_file：空白页不计费、彩色按覆盖率分档、
        # 双面按张。用户端预估、拼单结算与本接口共用同一份规则，价格不会走偏。
        skip_blank = bool(request_file.get("skip_blank", True))
        priced = pricing.price_file(
            pdf_path, print_range_start, print_range_end, print_copies,
            print_side, print_color, skip_blank, price_config)
        if priced is None:
            return ReturnResult(200, '所选范围内没有可打印内容', {})

        # 记录本文件的实付金额/省下金额/实际跳过的空白页，入队时按同一份页码打印。
        # 共享资料库优惠按文件逐个四舍五入，保证「各文件金额之和 = 账单总额」。
        priced_amount = round(float(priced["amount"]), 2)
        if library_rate > 0:
            amount = round(priced_amount * (1 - library_rate), 2)
            library_saved += round(priced_amount - amount, 2)
        else:
            amount = priced_amount
        request_file["amount"] = amount
        request_file["saved_amount"] = round(
            float(priced["saved_amount"]) + (priced_amount - amount), 2)
        request_file["skip_blank"] = skip_blank
        request_file["skip_blank_pages"] = priced["blank_pages"]
        price = price.add(Dinero(amount, CNY))
        bill_pages += priced["chargeable_count"] * print_copies
        bill_sheets += priced["sheets_per_copy"] * print_copies
        bill_saved += request_file["saved_amount"]


    out_trade_no = 'WX_' + datetime.now().strftime('%Y%m%d%H%M%S%f')
    # 取件码：6 位数字，只在用户端订单详情展示（不打印在纸上）。
    # 用 secrets 生成，避免 random 可预测导致他人猜出取件码领走他人物件。
    pickup_code = f"{secrets.randbelow(1000000):06d}"
    for file_attributes in request_body_attributes:
        file_attributes['pickup_code'] = pickup_code
    # 交付承诺：下单即按当前排队页数与本次页数估算预计耗时，给出承诺完成时间
    eta_seconds, eta_deadline = delivery.estimate_eta_seconds(printer_id, bill_pages)
    if scheduled_dt:
        # 预约单的承诺从「预约时间」起算：到点后预计多久打完，而不是从现在起算
        eta_deadline = (scheduled_dt
                        + timedelta(seconds=eta_seconds or 0)).strftime("%Y-%m-%d %H:%M:%S")
    bill_attributes = {'files_attributes': request_body_attributes, 'total_price': float(price.format()),
                       'out_trade_no': out_trade_no, 'expiry': time.time() + 60 * 60 * 3,
                       # 创建时间用于「15 秒内重复下单返回同一账单」的幂等判断
                       'created_ts': time.time(),
                       'printer_id': printer_id, 'pages': bill_pages, 'sheets': bill_sheets,
                       'pickup_code': pickup_code, 'saved': round(bill_saved, 2),
                       'eta_seconds': eta_seconds, 'eta_deadline': eta_deadline,
                       # 共享资料库：是否授权公开、分类、优惠金额（真实扣减）
                       'share_library': share_library,
                       'library_category': library_category,
                       'library_discount': round(library_saved, 2),
                       'library_discount_rate': round(library_rate, 4),
                       'library_contribution_rate': round(contribution_rate, 4),
                       'library_print_count': contribution.get("print_count", 0),
                       # 定时打印：预约出纸时间（空 = 立即打印）
                       'scheduled_at': scheduled_text,
                       # 记账单所属会话目录：付款提交时用它校验「这单是这次会话下的」，
                       # 不依赖前端传来的文件字段（前端是可信边界之外）
                       'directory': directory}

    if directory in files_attributes_global.data:
        files_attributes_global.data.pop(directory)
    else:
        return ReturnResult(200, '订单生成错误', {})

    # 账单先落库：这样即使支付中途失败，订单也不会丢，待支付的单还能继续付。
    db.save_bill(out_trade_no, bill_attributes)

    if not (APP_ID and ALIPAY_PRIVATE_KEY and ALIPAY_PUBLIC_KEY):
        # 当前收款方式：个人微信收款码（无需商户号），因此不返回支付宝支付链接，
        # 而是把「收款码 + 应付金额」交给用户端弹出扫码框；
        # 用户付款后调用 /pay/wechat/submit 入队，流水记为「待核验」。
        pay_config = db.get_pay_config()
        return ReturnResult(200, 'wechat payment required', {
            'out_trade_no': out_trade_no,
            'total_price': bill_attributes.get('total_price'),
            'pickup_code': pickup_code,
            'saved': bill_attributes.get('saved'),
            'eta_seconds': eta_seconds,
            'eta_deadline': eta_deadline,
            'scheduled_at': scheduled_text,
            'share_library': share_library,
            'library_discount': round(library_saved, 2),
            'library_discount_rate': round(library_rate, 4),
            'library_print_count': contribution.get("print_count", 0),
            'method': 'wechat_personal',
            'wechat_qr': pay_config.get('wechat_qr'),
            'payee': pay_config.get('payee'),
            'url': None,
        })

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        filemode="a",
    )
    logger = logging.getLogger("")

    alipay_client_config = AlipayClientConfig()
    alipay_client_config.server_url = "https://openapi.alipay.com/gateway.do"
    # 别看了，已失效
    alipay_client_config.app_id = APP_ID
    alipay_client_config.app_private_key = ALIPAY_PRIVATE_KEY
    alipay_client_config.alipay_public_key = ALIPAY_PUBLIC_KEY
    client = DefaultAlipayClient(alipay_client_config, logger)

    model = AlipayTradeCreateModel()
    model.out_trade_no = bill_attributes.get('out_trade_no')
    model.total_amount = bill_attributes.get('total_price')
    model.subject = "自助打印"
    model.timeout_express = '15m'
    to_encode = {'out_trade_no': bill_attributes.get('out_trade_no')}
    encode_jwt = encode(to_encode, SECRET_KEY, algorithm='HS256')
    model.body = encode_jwt

    request = AlipayTradePrecreateRequest(biz_model=model)

    request.notify_url = f"{SERVER_HOST}/pay/return"
    response_content = False
    try:
        response_content = client.execute(request)
    except Exception as e:
        print(traceback.format_exc())

    if not response_content:
        print("failed execute")
    else:
        response = AlipayTradeCreateResponse()
        string = response.parse_response_content(response_content)
        if response.is_success():
            result_code = response.code
            if not result_code:
                result_code = 0
            if int(result_code) == 10000:
                print("成功")
            else:
                return "https://qr.alipay.com/" + json.loads(response.body)['qr_code'].split("/")[-1]
            db.save_bill(out_trade_no, bill_attributes)
            return ReturnResult(200, 'success',
                                {'url': "https://qr.alipay.com/" + json.loads(response.body)['qr_code'].split("/")[-1]})
        else:
            print(
                f"{response.code}, {response.msg}, {response.sub_code}, {response.sub_msg}"
            )
        return {"message": "error"}


@router.post("/pay/return")
async def pay_return(trade_status: Annotated[str, Form()], out_trade_no: Annotated[str, Form()],
                     body: Annotated[str, Form()]):
    if not trade_status == 'TRADE_SUCCESS':
        return {"message": "error"}
    try:
        jwt_payload = jose.jwt.decode(body, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception as e:
        return {"message": "error"}
    if not jwt_payload.get('out_trade_no') == out_trade_no:
        return {"message": "error"}
    bill_attributes = db.get_bill(out_trade_no)
    if bill_attributes is None:
        return HTMLResponse(content='success', status_code=200)
    files_attributes = bill_attributes.get('files_attributes') or []
    # 源文件可能已被清理（上传目录长时间不活跃会被自动清理）。文件没了就不能入队，
    # 否则 _enqueue_paid_order 里 os.replace 会抛异常、回调变 500。
    # 支付宝要求回调必须回 success，故这里记录日志、跳过入队并如实回 success。
    missing = False
    for item in files_attributes:
        source_name = (item.get('filename') or "").rsplit(".", 1)[0] + ".pdf"
        if not os.path.isfile(f"uploads/{item.get('folder')}/converted/{source_name}"):
            logging.error("pay_return: source file missing, skip enqueue for %s",
                          out_trade_no)
            missing = True
            break
    if missing:
        db.delete_bill(out_trade_no)
        return HTMLResponse(content='success', status_code=200)

    # 先入队、成功后再删账单：入队异常时账单还在，可人工补单，
    # 不会出现「钱已付、账单已删、订单没落库」
    try:
        _enqueue_paid_order(files_attributes, out_trade_no,
                            eta=_bill_eta(bill_attributes),
                            share_library=bool(bill_attributes.get('share_library')),
                            library_category=(bill_attributes.get('library_category')
                                              or '其他'))
    except Exception:
        logging.exception("pay_return: enqueue failed, bill kept for manual retry %s",
                          out_trade_no)
        return HTMLResponse(content='success', status_code=200)
    db.delete_bill(out_trade_no)
    return HTMLResponse(content='success', status_code=200)


@router.post("/pay/wechat/submit")
async def pay_wechat_submit(request_body: WechatPaySubmit,
                            payload: dict = Depends(jwt.verify_token)):
    """个人微信收款码付款后，用户点「我已支付」提交订单。

    如实说明：个人收款码没有商户回调，后端无法自动确认到账，因此这里
    - 立即把订单入队（用户已付款，不该因为人工核对而卡住取件），
    - 同时写一条收款流水，状态为 submitted（用户声明已付、待管理端核对）；
    管理端「账单流水」页核对到账后点「确认收款」将其改为 confirmed。
    鉴权与归属校验：必须携带下单时的会话 JWT，且账单所属会话目录必须与 token 一致，
    防止有人拿别人的订单号跳过付款（目录取创建账单时记录的值，不依赖前端传参）。
    """
    out_trade_no = request_body.out_trade_no
    # get_bill 内部处理过期：过期会自动删除并返回 None
    bill_attributes = db.get_bill(out_trade_no)
    if bill_attributes is None:
        return ReturnResult(200, "order not found or expired", {})
    files_attributes = bill_attributes.get('files_attributes') or []
    owned = bill_attributes.get('directory') == payload.get("token")
    # 拼单按账号归属：换会话/换设备后仍可支付自己的份额或自己发起的整单
    bill_kind = bill_attributes.get('kind')
    if not owned and bill_kind in ('group_member', 'group'):
        owner_field = 'member_uid' if bill_kind == 'group_member' else 'owner_uid'
        owned = bool(payload.get("uid")) and str(
            bill_attributes.get(owner_field)) == str(payload.get("uid"))
    if not owned:
        return ReturnResult(200, "order not owned by this session", {})
    # 上传目录 15 分钟不活跃会被清理；文件没了就不能再入队，
    # 否则 os.replace 会抛异常变成 500，用户还已经付了钱
    for item in files_attributes:
        source_name = (item.get('filename') or "").rsplit(".", 1)[0] + ".pdf"
        if not os.path.isfile(f"uploads/{item.get('folder')}/converted/{source_name}"):
            return ReturnResult(200, "文件已过期，请重新上传后再支付", {})
    # 先入队，成功后再移除账单：入队抛异常时账单还在，用户可重试，
    # 不会出现「钱已付、账单已删、订单没落库」。
    # 流水摘要先取：入队时会把文件名改成转换后的 .pdf，这里保留用户上传时的原名便于对账
    files_summary = _files_summary(files_attributes)
    eta = _bill_eta(bill_attributes)
    _enqueue_paid_order(files_attributes, out_trade_no, eta=eta,
                        share_library=bool(bill_attributes.get('share_library')),
                        library_category=(bill_attributes.get('library_category')
                                          or '其他'))
    db.delete_bill(out_trade_no)
    # AA 拼单：本成员支付成功，标记已付；全部付完把拼单状态改为 paid
    if bill_attributes.get('kind') == 'group_member':
        group_no = db.mark_group_member_paid(out_trade_no)
        if group_no:
            db.refresh_group_paid_status(group_no)
    # 写收款流水：金额/设备/页数/张数都留痕，管理端按此对账
    payment_id = db.add_payment(
        out_trade_no,
        bill_attributes.get('total_price'),
        device_id=bill_attributes.get('printer_id'),
        method='wechat_personal',
        status='submitted',
        detail=files_summary,
        pages=bill_attributes.get('pages'),
        sheets=bill_attributes.get('sheets'),
        payer_note=request_body.payer_note,
    )
    scheduled_at = bill_attributes.get('scheduled_at')
    return ReturnResult(200, "success", {
        "out_trade_no": out_trade_no,
        "payment_id": payment_id,
        "amount": bill_attributes.get('total_price'),
        "pickup_code": bill_attributes.get('pickup_code'),
        "eta_seconds": eta[0],
        "eta_deadline": eta[1],
        "scheduled_at": scheduled_at,
        "status": "submitted",
        "note": ("已预约，到点才排队打印。" if scheduled_at
                 else "订单已入队；收款状态为「待核验」，由管理员核对到账后确认。"),
    })
