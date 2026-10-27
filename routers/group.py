"""拼单打印 API。

一个人发起 → 生成 6 位短码与分享链接 → 发到宿舍群 / 课程群 →
同学扫码打开 /?group=XXXXXX，各自上传文件、填姓名加入 →
人数越多折扣越低（3 人 9 折、5 人 8 折、10 人 7 折）→
发起人统一付款、统一取件，每人一份带姓名封皮的文件（便于分拣）。

设计取舍：
- 成员文件在「加入」时复制到 uploads/group_{短码}/converted/，
  不再依赖各自的会话目录，避免 15 分钟不活跃清理把文件删掉。
- 计价复用 pricing.price_file，与用户端预估、普通下单结算同一套规则。
- 结算沿用 pay.py 的收款码链路（账单落在 bills 单例里，
  /pay/wechat/submit 付款后入队），入队时按 mode=group 给每人加姓名封皮。
"""

import os
import secrets
import shutil
from datetime import datetime
from typing import Annotated, Optional

import fitz
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

import db
import delivery
import jwt
import pricing
from models import ReturnResult

router = APIRouter()

# 阶梯折扣：人数达到门槛就享受对应折扣（取满足条件的最大档）
DISCOUNT_LADDER = [(10, 0.7), (5, 0.8), (3, 0.9)]
# 分享短码字符集：去掉 0/O/1/I 这类易混字符，方便同学口头转述
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ALLOWED_SIDES = ("one-sided", "two-sided-default",
                 "two-sided-long-edge", "two-sided-short-edge")
ALLOWED_PAPER = ("A4", "A5", "A6", "LETTER")


def discount_for(count):
    """按人数取折扣系数；1~2 人原价。"""
    for need, factor in DISCOUNT_LADDER:
        if count >= need:
            return factor
    return 1.0


def next_tier(count):
    """下一个折扣档位，用于展示「还差 X 人再打折」。"""
    for need, factor in sorted(DISCOUNT_LADDER):
        if count < need:
            return {"need": need, "factor": factor, "more": need - count}
    return None


def _new_group_no():
    """生成一个库中不存在的 6 位短码。

    短码是拼单分享凭据，猜测它即可查看/干扰他人拼单，故用 secrets 生成，
    避免 random 可预测。_ALPHABET 去掉了易混淆字符，取模不会造成明显偏置。
    """
    for _ in range(20):
        code = ''.join(secrets.choice(_ALPHABET) for _ in range(6))
        if db.get_group(code) is None:
            return code
    # 极端情况（几乎不可能）：加长短码长度兜底，同样要查重后才返回
    for _ in range(20):
        code = ''.join(secrets.choice(_ALPHABET) for _ in range(8))
        if db.get_group(code) is None:
            return code
    raise RuntimeError("无法生成唯一拼单短码")


def _safe_filename(filename):
    """只保留文件名本身，挡住 ../ 路径穿越。"""
    name = os.path.basename((filename or "").replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        return None
    return name


def _group_folder(group_no):
    """拼单成员文件统一存放在 uploads/group_{短码}/converted/ 下。"""
    return f"group_{group_no}"


def _key_from_header(authorization):
    """从 Authorization 头里取出会话目录（短码）；无效则返回 None。

    用于 GET 时标记「哪几条是我加入的」以及退出拼单的归属校验。
    """
    return _requester_from_header(authorization)[0]


def _requester_from_header(authorization):
    """返回 (会话目录, 登录用户 id)；未登录或无效时为 (None, None)。"""
    if not authorization:
        return None, None
    try:
        token = authorization.split(' ')[1]
        payload = jwt.decode_token(token)
        return payload.get("token"), payload.get("uid")
    except Exception:
        return None, None


def _owner_matches(group, requester_key, requester_uid):
    """判断请求者是否是发起人：优先按账号 uid，老数据回退到会话目录 key。

    登录 token 每次重新登录都会换会话目录，只比 owner_key 会让发起人换设备后
    失去锁定/结算权限，因此有 uid 时以 uid 为准。
    """
    if requester_uid is not None and group.get("owner_uid") is not None:
        return str(group["owner_uid"]) == str(requester_uid)
    return bool(requester_key) and group.get("owner_key") == requester_key


def _group_state(group, requester_key=None, requester_uid=None):
    """把拼单 + 成员汇总成前端需要的结构。"""
    members = db.list_group_members(group["group_no"])
    count = len(members)
    discount = discount_for(count)
    raw_total = round(sum(m["amount"] for m in members), 2)
    total = round(raw_total * discount, 2)
    # AA 锁定后金额已经冻结，不能再随人数变化
    if group["status"] in ("collecting", "paid") and group.get("total_price") is not None:
        discount = group.get("discount") or discount
        total = round(group["total_price"], 2)
    saved = round(sum(m["saved_amount"] for m in members)
                  + (raw_total - total), 2)
    out_members = []
    my_member = None
    for m in members:
        mine = bool((requester_key and m["folder"] == requester_key)
                    or (requester_uid and str(m.get("user_id")) == str(requester_uid)))
        if mine:
            my_member = m
        out_members.append({
            "id": m["id"],
            "name": m["name"],
            "pages": m["pages"],
            "copies": m["print_copies"],
            "side": m["print_side"],
            "color": m["print_color"],
            "paper": m["paper"],
            "amount": m["amount"],
            "payable": m.get("payable"),
            "paid": bool(m.get("paid")),
            "pickup_code": m.get("pickup_code") if mine else None,
            "joined_at": m["created_at"],
            "mine": mine,
        })
    my_bill = None
    if my_member and my_member.get("payable") is not None:
        out_trade_no = my_member.get("out_trade_no")
        my_bill = {
            "out_trade_no": out_trade_no,
            "total_price": my_member["payable"],
            "pickup_code": my_member.get("pickup_code"),
            "paid": bool(my_member.get("paid")),
            "expired": bool(out_trade_no and not my_member.get("paid")
                            and db.get_bill(out_trade_no) is None),
        }
    return {
        "group_no": group["group_no"],
        "owner_name": group["owner_name"],
        "device_id": group["device_id"],
        "status": group["status"],
        "pay_mode": group.get("pay_mode") or "unified",
        "created_at": group["created_at"],
        "member_count": count,
        "paid_count": sum(1 for m in members if m.get("paid")),
        "members": out_members,
        "raw_total": raw_total,
        "discount": discount,
        "total_price": total,
        "saved": saved,
        "next_tier": next_tier(count),
        "ladder": [{"count": need, "factor": factor}
                   for need, factor in sorted(DISCOUNT_LADDER)],
        "pickup_code": group.get("pickup_code"),
        "order_no": group.get("order_no"),
        "my_bill": my_bill,
        "is_owner": _owner_matches(group, requester_key, requester_uid),
    }


class CreateGroup(BaseModel):
    """发起拼单请求体。pay_mode=aa 每人付自己的份额；unified 发起人统一付。"""

    owner_name: str
    printer_id: Optional[str] = None
    pay_mode: str = "aa"


class JoinGroup(BaseModel):
    """加入拼单请求体：姓名 + 自己的文件与打印参数。"""

    group_no: str
    name: str
    filename: str
    print_copies: int = 1
    print_range_start: int = 1
    print_range_end: Optional[int] = None
    print_side: str = "one-sided"
    print_color: str = "bw"
    paper: str = "A4"
    skip_blank: bool = True


@router.post("/api/group/create")
def group_create(request: CreateGroup, payload: dict = Depends(jwt.verify_token)):
    """发起拼单：返回短码与初始状态（分享链接由前端按当前域名拼）。"""
    name = (request.owner_name or "").strip()
    if not name:
        return ReturnResult(200, "请填写发起人姓名", {})
    if len(name) > 20:
        return ReturnResult(200, "姓名太长（最多 20 字）", {})
    group_no = _new_group_no()
    db.create_group(group_no, name, payload.get("token"),
                    (request.printer_id or None),
                    pay_mode=(request.pay_mode or "aa"),
                    owner_uid=payload.get("uid"))
    group = db.get_group(group_no)
    return ReturnResult(200, "success",
                        _group_state(group, payload.get("token"), payload.get("uid")))


@router.get("/api/group/{group_no}")
def group_detail(group_no: str,
                 Authorization: Annotated[str | None, Header()] = None):
    """查看拼单进度（公开：扫码进来的人不需要登录即可看到人数与价格）。"""
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    key, uid = _requester_from_header(Authorization)
    return ReturnResult(200, "success", _group_state(group, key, uid))


@router.post("/api/group/join")
def group_join(request: JoinGroup, payload: dict = Depends(jwt.verify_token)):
    """加入拼单：把自己的文件复制进拼单目录并计价。"""
    folder = payload.get("token")
    group_no = (request.group_no or "").strip().upper()
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    if group["status"] != "open":
        return ReturnResult(200, "拼单已结算，不能再加入", {})

    name = (request.name or "").strip()
    if not name:
        return ReturnResult(200, "请填写你的姓名（会印在封皮上）", {})
    if len(name) > 20:
        return ReturnResult(200, "姓名太长（最多 20 字）", {})

    safe_name = _safe_filename(request.filename)
    if safe_name is None:
        return ReturnResult(200, "文件名不合法", {})
    base = safe_name.rsplit(".", 1)[0]
    source_path = f"uploads/{folder}/converted/{base}.pdf"
    if not os.path.isfile(source_path):
        return ReturnResult(200, "文件还没转换完成，请稍后再点加入", {})

    try:
        doc = fitz.open(source_path)
        total_pages = doc.page_count
        doc.close()
    except Exception:
        return ReturnResult(200, "文件无法读取，请重新上传", {})
    if total_pages < 1:
        return ReturnResult(200, "文件没有可打印页面", {})

    try:
        start = max(1, int(request.print_range_start or 1))
        end = int(request.print_range_end or total_pages)
    except (TypeError, ValueError):
        return ReturnResult(200, "打印范围不合法", {})
    end = min(end, total_pages)
    if end < start:
        return ReturnResult(200, "打印范围不合法", {})
    copies = max(1, min(int(request.print_copies or 1), 100))
    side = request.print_side if request.print_side in ALLOWED_SIDES else "one-sided"
    color = request.print_color if request.print_color in ("bw", "color") else "bw"
    paper = (request.paper or "A4").strip().upper()
    if paper not in ALLOWED_PAPER:
        paper = "A4"
    skip_blank = bool(request.skip_blank)

    # 复制到拼单专属目录：不依赖成员自己的会话，避免 15 分钟清理丢文件
    gfolder = _group_folder(group_no)
    # 同一账号重复加入（可能换了会话）：先删掉旧成员与旧文件，避免一人占两行
    for old in db.delete_group_members_by_user(group_no, payload.get("uid")):
        old_path = f"uploads/{gfolder}/converted/{old['filename']}"
        if os.path.isfile(old_path):
            os.remove(old_path)
    # 同一会话重复加入：先删掉旧的成员文件，避免成员被覆盖后留下孤儿文件
    for old in db.list_group_members(group_no):
        if old.get("folder") == folder:
            old_path = f"uploads/{gfolder}/converted/{old['filename']}"
            if os.path.isfile(old_path):
                os.remove(old_path)
    os.makedirs(f"uploads/{gfolder}/converted", exist_ok=True)
    stored_name = f"{group_no}_{os.urandom(4).hex()}_{base}.pdf"
    stored_path = f"uploads/{gfolder}/converted/{stored_name}"
    shutil.copyfile(source_path, stored_path)

    price_cfg = db.effective_price(group.get("device_id"))
    priced = pricing.price_file(stored_path, start, end, copies, side, color,
                               skip_blank, price_cfg)
    if priced is None:
        if os.path.exists(stored_path):
            os.remove(stored_path)
        return ReturnResult(200, "所选范围内没有可打印内容", {})

    db.add_group_member(
        group_no, name, folder, stored_name, copies, start, end,
        side, color, paper, skip_blank, priced["chargeable_count"],
        priced["amount"], priced["saved_amount"], priced["blank_pages"],
        user_id=payload.get("uid"))
    return ReturnResult(200, "success",
                        _group_state(db.get_group(group_no), folder,
                                     payload.get("uid")))


@router.post("/api/group/leave/{group_no}")
def group_leave(group_no: str, payload: dict = Depends(jwt.verify_token)):
    """退出拼单：删掉本会话加入的成员及其文件。"""
    group_no = (group_no or "").strip().upper()
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    if group["status"] != "open":
        return ReturnResult(200, "拼单已结算，不能退出", {})
    folder = payload.get("token")
    # 同一账号可能换了会话目录再打开：按账号与会话各删一次，去重后清理文件
    removed = {m["id"]: m for m in
               db.delete_group_members_by_folder(group_no, folder)}
    for m in db.delete_group_members_by_user(group_no, payload.get("uid")):
        removed[m["id"]] = m
    gfolder = _group_folder(group_no)
    for m in removed.values():
        path = f"uploads/{gfolder}/converted/{m['filename']}"
        if os.path.isfile(path):
            os.remove(path)
    return ReturnResult(200, "success",
                        _group_state(db.get_group(group_no), folder,
                                     payload.get("uid")))


@router.post("/api/group/remove/{group_no}/{member_id}")
def group_remove(group_no: str, member_id: int,
                 payload: dict = Depends(jwt.verify_token)):
    """发起人移除某个成员（结算前用于剔除误加入的成员）。"""
    group_no = (group_no or "").strip().upper()
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    if not _owner_matches(group, payload.get("token"), payload.get("uid")):
        return ReturnResult(200, "只有发起人可以移除成员", {})
    if group["status"] != "open":
        return ReturnResult(200, "拼单已结算，不能移除成员", {})
    members = db.list_group_members(group_no)
    hit = next((m for m in members if m["id"] == member_id), None)
    if hit is None:
        return ReturnResult(200, "成员不存在", {})
    db.delete_group_member(member_id)
    path = f"uploads/{_group_folder(group_no)}/converted/{hit['filename']}"
    if os.path.isfile(path):
        os.remove(path)
    return ReturnResult(200, "success",
                        _group_state(db.get_group(group_no), payload.get("token"),
                                     payload.get("uid")))


def _member_bill(group, member, payable, out_trade_no, pickup_code, discount):
    """为 AA 拼单里的一个成员生成独立账单：每人一份、各自取件。"""
    copies = int(member["print_copies"] or 1)
    pages = int(member["pages"] or 0) * copies
    if member["print_side"] == "one-sided":
        sheets = int(member["pages"] or 0) * copies
    else:
        sheets = ((int(member["pages"] or 0) + 1) // 2) * copies
    eta_seconds, eta_deadline = delivery.estimate_eta_seconds(
        group.get("device_id"), pages)
    files_attributes = [{
        "folder": _group_folder(group["group_no"]),
        "filename": member["filename"],
        "user_id": member.get("user_id"),
        "display_name": f"{member['name']} 的文件.pdf",
        "print_copies": copies,
        "print_range_start": member["print_range_start"],
        "print_range_end": member["print_range_end"],
        "print_side": member["print_side"],
        "print_color": member["print_color"],
        "paper": member["paper"],
        "skip_blank": member["skip_blank"],
        "skip_blank_pages": member["blank_pages"],
        "pickup_code": pickup_code,
        "printer_id": group.get("device_id"),
        "out_trade_no": out_trade_no,
        "amount": payable,
        "saved_amount": round(member["saved_amount"]
                              + member["amount"] * (1 - discount), 2),
        # 每人一份姓名封皮，各自取件时按姓名分拣
        "batch": {"mode": "group", "names": [member["name"]] * copies,
                  "owner": group["owner_name"]},
    }]
    return {
        "files_attributes": files_attributes,
        "total_price": payable,
        "out_trade_no": out_trade_no,
        "expiry": datetime.now().timestamp() + 60 * 60 * 3,
        "printer_id": group.get("device_id"),
        "pages": pages,
        "sheets": sheets,
        "pickup_code": pickup_code,
        "saved": round(member["saved_amount"]
                       + member["amount"] * (1 - discount), 2),
        "eta_seconds": eta_seconds,
        "eta_deadline": eta_deadline,
        # 账单归属该成员自己的会话，别人无法代付/冒领
        "directory": member["folder"],
        "group_no": group["group_no"],
        "kind": "group_member",
        "member_id": member["id"],
        "member_uid": member.get("user_id"),
        "member_name": member["name"],
    }


@router.post("/api/group/collect/{group_no}")
def group_collect(group_no: str, payload: dict = Depends(jwt.verify_token)):
    """AA 拼单：发起人锁定人数，系统给每个成员生成自己的账单和取件码。

    锁定后不能再加入；每个成员从自己的会话支付自己的份额，付完即可各自取件。
    """
    group_no = (group_no or "").strip().upper()
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    if not _owner_matches(group, payload.get("token"), payload.get("uid")):
        return ReturnResult(200, "只有发起人可以锁定拼单", {})
    if (group.get("pay_mode") or "unified") != "aa":
        return ReturnResult(200, "当前是统一付款拼单，不需要锁定", {})
    if group["status"] != "open":
        return ReturnResult(200, "拼单已锁定或已完成", {})

    members = db.list_group_members(group_no)
    if not members:
        return ReturnResult(200, "还没有人加入，先把链接发给大家", {})

    gfolder = _group_folder(group_no)
    for m in members:
        if not os.path.isfile(f"uploads/{gfolder}/converted/{m['filename']}"):
            return ReturnResult(200, f"「{m['name']}」的文件已丢失，请让他重新加入", {})

    # 原子占位：连点/并发时只有一次能锁定，避免生成两套账单导致重复付款
    if not db.claim_group_for_collect(group_no):
        return ReturnResult(200, "拼单已锁定或已完成", {})

    discount = discount_for(len(members))
    raw_total = round(sum(m["amount"] for m in members), 2)
    total_price = round(raw_total * discount, 2)
    # 按成员金额比例分摊，四舍五入差额补到第一位，保证合计与总价一致
    shares = [round(m["amount"] * discount, 2) for m in members]
    diff = round(total_price - sum(shares), 2)
    shares[0] = round(shares[0] + diff, 2)

    for member, payable in zip(members, shares):
        out_trade_no = ("GA_" + group_no + "_" + str(member["id"]) + "_"
                        + datetime.now().strftime("%H%M%S%f")
                        + secrets.token_hex(2))
        pickup_code = f"{secrets.randbelow(1000000):06d}"
        bill = _member_bill(group, member, payable, out_trade_no,
                            pickup_code, discount)
        db.save_bill(out_trade_no, bill)
        db.set_group_member_bill(member["id"], payable, out_trade_no,
                                 pickup_code)

    db.update_group(group_no, status="collecting", discount=discount,
                    total_price=total_price, order_no=None,
                    pickup_code=None)
    return ReturnResult(200, "success",
                        _group_state(db.get_group(group_no),
                                     payload.get("token"),
                                     payload.get("uid")))


@router.post("/api/group/settle/{group_no}")
def group_settle(group_no: str, payload: dict = Depends(jwt.verify_token)):
    """发起人统一结算：按人数打折后生成一份账单，走收款码付款。"""
    group_no = (group_no or "").strip().upper()
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    # 归属校验必须放在最前：已结算分支会下发收款码/订单号等敏感信息，
    # 若不校验，任何拿到 group_no 的会话都能直接获取发起人的收款码与订单号。
    if not _owner_matches(group, payload.get("token"), payload.get("uid")):
        return ReturnResult(200, "只有发起人可以结算", {})
    if (group.get("pay_mode") or "unified") == "aa":
        return ReturnResult(200, "当前是 AA 拼单，请先锁定并让每人支付自己的份额", {})
    if group["status"] != "open":
        # 已结算：账单仍在单例里 → 返回同一收款码，发起人可再次打开；
        # 账单已被付款/过期移除 → 明确告知，避免重复生成账单造成重复打印。
        out_trade_no = group.get("order_no")
        bill = db.get_bill(out_trade_no)
        if bill is None:
            return ReturnResult(200, "拼单已付款完成", {})
        pay_config = db.get_pay_config()
        return ReturnResult(200, "wechat payment required", {
            'out_trade_no': out_trade_no,
            'total_price': group.get("total_price") or bill.get("total_price"),
            'pickup_code': group.get("pickup_code"),
            'saved': bill.get('saved'),
            'eta_seconds': bill.get('eta_seconds'),
            'eta_deadline': bill.get('eta_deadline'),
            'method': 'wechat_personal',
            'wechat_qr': pay_config.get('wechat_qr'),
            'payee': pay_config.get('payee'),
            'url': None,
            'group_no': group_no,
        })

    members = db.list_group_members(group_no)
    if not members:
        return ReturnResult(200, "还没有人加入，先把链接发给大家", {})

    gfolder = _group_folder(group_no)
    for m in members:
        if not os.path.isfile(f"uploads/{gfolder}/converted/{m['filename']}"):
            return ReturnResult(200, f"「{m['name']}」的文件已丢失，请让他重新加入", {})

    discount = discount_for(len(members))
    raw_total = round(sum(m["amount"] for m in members), 2)
    total_price = round(raw_total * discount, 2)
    saved = round(sum(m["saved_amount"] for m in members)
                  + (raw_total - total_price), 2)
    printer_id = group.get("device_id")
    out_trade_no = 'GP_' + datetime.now().strftime('%Y%m%d%H%M%S%f')
    pickup_code = f"{secrets.randbelow(1000000):06d}"

    files_attributes = []
    pages = 0
    sheets = 0
    owner_uid = payload.get("uid")
    for m in members:
        copies = m["print_copies"]
        files_attributes.append({
            "folder": gfolder,
            "filename": m["filename"],
            # 拼单由发起人付款：订单统一归到发起人账号下
            "user_id": int(owner_uid) if owner_uid else None,
            "display_name": f"{m['name']} 的文件.pdf",
            "print_copies": copies,
            "print_range_start": m["print_range_start"],
            "print_range_end": m["print_range_end"],
            "print_side": m["print_side"],
            "print_color": m["print_color"],
            "paper": m["paper"],
            "skip_blank": m["skip_blank"],
            "skip_blank_pages": m["blank_pages"],
            "pickup_code": pickup_code,
            "printer_id": printer_id,
            "out_trade_no": out_trade_no,
            # 金额按折后均摊到各成员，管理端对账时可加总核对
            "amount": round(m["amount"] * discount, 2),
            "saved_amount": round(m["saved_amount"] + m["amount"] * (1 - discount), 2),
            # mode=group：入队时给每人加一张姓名封皮，便于按姓名分拣
            "batch": {"mode": "group",
                      "names": [m["name"]] * copies,
                      "owner": group["owner_name"]},
        })
        pages += m["pages"] * copies
        sheets += ((m["pages"] + 1) // 2
                   if m["print_side"] != "one-sided" else m["pages"]) * copies

    # 交付承诺：下单即按当前排队页数与本单页数给出承诺完成时间（超时免单基线）
    eta_seconds, eta_deadline = delivery.estimate_eta_seconds(printer_id, pages)
    bill_attributes = {
        'files_attributes': files_attributes,
        'total_price': total_price,
        'out_trade_no': out_trade_no,
        'expiry': datetime.now().timestamp() + 60 * 60 * 3,
        'printer_id': printer_id,
        'pages': pages,
        'sheets': sheets,
        'pickup_code': pickup_code,
        'saved': round(saved, 2),
        'eta_seconds': eta_seconds,
        'eta_deadline': eta_deadline,
        # 账单归属会话目录：付款提交时用它校验「这单是发起人下的」
        'directory': group["owner_key"],
        'group_no': group_no,
        'kind': 'group',
        # 发起人账号：重新登录后会话目录会变，付款时按 uid 兜底认领
        'owner_uid': owner_uid,
    }
    db.save_bill(out_trade_no, bill_attributes)
    db.update_group(group_no, status='settled', discount=discount,
                    total_price=total_price, order_no=out_trade_no,
                    pickup_code=pickup_code,
                    settled_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    pay_config = db.get_pay_config()
    return ReturnResult(200, "wechat payment required", {
        'out_trade_no': out_trade_no,
        'total_price': total_price,
        'pickup_code': pickup_code,
        'saved': round(saved, 2),
        'eta_seconds': eta_seconds,
        'eta_deadline': eta_deadline,
        'method': 'wechat_personal',
        'wechat_qr': pay_config.get('wechat_qr'),
        'payee': pay_config.get('payee'),
        'url': None,
        'group_no': group_no,
    })


@router.post("/api/group/reopen/{group_no}")
def group_reopen(group_no: str, payload: dict = Depends(jwt.verify_token)):
    """取消未付款的结算，让拼单能继续加人或重新结算。

    只在该次账单还没被付款时允许（账单仍在 bills 单例里），
    避免已经付款的单被重复结算导致重复打印。
    """
    group_no = (group_no or "").strip().upper()
    group = db.get_group(group_no)
    if group is None:
        return ReturnResult(200, "拼单不存在或已过期", {})
    if not _owner_matches(group, payload.get("token"), payload.get("uid")):
        return ReturnResult(200, "只有发起人可以操作", {})
    if (group.get("pay_mode") or "unified") == "aa":
        members = db.list_group_members(group_no)
        if any(m.get("paid") for m in members):
            return ReturnResult(200, "已有成员付款，不能取消；未付款成员可继续支付", {})
        for member in members:
            if member.get("out_trade_no"):
                db.delete_bill(member["out_trade_no"])
        db.reset_group_member_bills(group_no)
        db.update_group(group_no, status="open", discount=None,
                        total_price=None, order_no=None,
                        pickup_code=None, settled_at=None)
        return ReturnResult(200, "success",
                            _group_state(db.get_group(group_no),
                                         payload.get("token"),
                                         payload.get("uid")))
    out_trade_no = group.get("order_no")
    if out_trade_no and db.get_bill(out_trade_no) is None:
        return ReturnResult(200, "账单已支付或已过期，不能取消结算", {})
    db.delete_bill(out_trade_no)
    db.update_group(group_no, status='open', total_price=None, order_no=None,
                    pickup_code=None, settled_at=None)
    return ReturnResult(200, "success",
                        _group_state(db.get_group(group_no), payload.get("token"),
                                     payload.get("uid")))
