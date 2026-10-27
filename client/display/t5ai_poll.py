"""把后端真实数据推给 T5 取件屏（串口 JSON 行）。

用法：
  python t5ai_poll.py --device printer-001 --port COM4
  python t5ai_poll.py --device printer-001 --port COM4 --backend https://your.domain:8000

取数优先级：
  1) GET /api/display/{device_id}/state  —— 一次拿全（状态/队列/纸量/取件码/单价）
  2) 若后端是旧版（该接口 404），降级为 GET /api/price + GET /api/device/{id}
     此时「取件码」拿不到，只能区分 idle / busy
后端取数失败时不发帧，板子 6 秒收不到帧会自动置灰并保留最后一帧。
单价按「分」下发（0.3 元 -> 30），板子端自己格式化成 0.30。
"""

import argparse
import json
import os
import sys
import time

import requests
import serial


def fetch_state(session, base, device, headers, timeout):
    resp = session.get("%s/api/display/%s/state" % (base, device),
                       headers=headers, timeout=timeout)
    if resp.status_code == 200:
        return resp.json(), "display"
    if resp.status_code != 404:
        raise RuntimeError("display %d %s" % (resp.status_code, resp.text[:120]))

    price = session.get("%s/api/price" % base, params={"device_id": device},
                        timeout=timeout)
    price.raise_for_status()
    bw = (price.json().get("data") or {}).get("bw")

    depth, sheets = 0, None
    detail = session.get("%s/api/device/%s" % (base, device),
                         headers=headers, timeout=timeout)
    if detail.status_code == 200:
        dev = (detail.json().get("data") or {}).get("device") or {}
        depth = int(dev.get("queue_depth") or 0)
        sheets = dev.get("paper_left")
        return {
            "state": "busy" if depth > 0 else "idle",
            "queue_len": depth,
            "paper": {"sheets": sheets},
            "pickup": {"code": ""},
            "price": {"bw": bw},
            "ts": int(time.time()),
        }, "legacy"

    return {
        "state": "idle",
        "queue_len": -1,
        "paper": {"sheets": -1},
        "pickup": {"code": ""},
        "price": {"bw": bw},
        "ts": int(time.time()),
    }, "price-only"


def send_heartbeat(session, base, device, headers, timeout, sheets):
    """顺手给后端发心跳，让管理端设备页显示真实在线。

    纸量取 display 接口返回的 paper_left，仅在有效时上报（-1 表示未知，不冒充）。
    """
    payload = {"device_id": device, "status": "online"}
    if sheets is not None and int(sheets) >= 0:
        payload["paper_left"] = int(sheets)
    try:
        resp = session.post("%s/api/device/heartbeat" % base, json=payload,
                            headers=headers, timeout=timeout)
        return resp.status_code == 200
    except Exception as exc:
        print("heartbeat failed:", exc)
        return False


def build_frame(data):
    """把后端返回拼成一帧串口数据。

    p / u 拿不到时下发 -1，让屏幕显示「--」，而不是沿用上一帧的旧数字
    （旧值冒充新值就是假数据，宁可不显示）。
    """
    price = (data.get("price") or {}).get("bw")
    sheets = (data.get("paper") or {}).get("sheets")
    frame = {
        "s": data.get("state", "idle"),
        "q": int(data.get("queue_len") or 0),
        "c": str((data.get("pickup") or {}).get("code") or ""),
        "t": int(data.get("ts") or time.time()),
        "p": int(sheets) if sheets is not None else -1,
        "u": int(round(float(price) * 100)) if price is not None else -1,
    }
    return frame


def parse_board_line(raw):
    """解析板子回行；只认 {"a":"pick","c":"取件码"}，其余一律忽略（日志行、乱码）。"""
    try:
        msg = json.loads(raw.decode("utf-8", "ignore").strip())
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("a") != "pick":
        return None
    code = str(msg.get("c") or "").strip()
    return code or None


def confirm_pickup(session, base, device, headers, timeout, code):
    resp = session.post("%s/api/display/%s/pickup" % (base, device),
                        json={"code": code}, headers=headers, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


def pickup_reply(status, body):
    """把确认结果翻成回给板子的一行：{"r":"pick","ok":0/1,"m":"提示语"}。

    板子只做展示，不做业务判断；点错了/码过期了要让用户当场看到，
    否则屏幕只会一直停在「请取件」，用户会以为没点中。
    """
    if status == 200:
        if body.get("duplicate"):
            return {"r": "pick", "ok": 1, "m": "已取件 请勿重复确认"}
        return {"r": "pick", "ok": 1, "m": "已取件 请拿好纸张"}
    if status == 404:
        return {"r": "pick", "ok": 0, "m": "取件码已失效"}
    if status == 429:
        return {"r": "pick", "ok": 0, "m": "操作太频繁 请稍等"}
    if status in (401, 403):
        return {"r": "pick", "ok": 0, "m": "终端未授权 请联系管理员"}
    return {"r": "pick", "ok": 0, "m": "取件失败 请重试"}


def send_line(ser, payload):
    """整行写出，返回 True 表示写成功。"""
    if ser is None or not ser.is_open:
        return False
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ser.write((line + "\n").encode("utf-8"))
    return True


def drain_board(ser, buf, session, base, device, headers, timeout):
    """读取板子回行并转发取件确认；返回剩余未成行的缓冲。"""
    if ser is None or not ser.is_open or ser.in_waiting <= 0:
        return buf
    buf += ser.read(ser.in_waiting)
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        code = parse_board_line(line)
        if code is None:
            continue
        status, body = confirm_pickup(session, base, device, headers, timeout, code)
        reply = pickup_reply(status, body)
        send_line(ser, reply)
        print("pick %s -> %d %s | tx %s" % (
            code, status, json.dumps(body, ensure_ascii=False), reply["m"]))
    return buf[-512:]


def open_serial(port, baud):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True)
    ap.add_argument("--port", required=True, help="板子 UART0 所在串口，如 COM4 或 /dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--backend", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default=os.getenv("PRINTER_KEY") or os.getenv("SECRET_KEY") or "")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--timeout", type=float, default=6.0)
    ap.add_argument("--heartbeat-interval", type=float, default=30.0,
                    help="心跳间隔（秒），0 表示不发心跳")
    args = ap.parse_args()

    if not args.key:
        sys.exit("缺少密钥：--key 或环境变量 PRINTER_KEY / SECRET_KEY")

    base = args.backend.rstrip("/")
    headers = {"Authorization": "Bearer " + args.key}
    session = requests.Session()

    ser = open_serial(args.port, args.baud)
    print("bridge up: %s device=%s -> %s @%d" % (base, args.device, args.port, args.baud))

    mode = None
    last_hb = 0.0
    rx_buf = b""
    while True:
        started = time.time()
        try:
            rx_buf = drain_board(ser, rx_buf, session, base, args.device, headers,
                                 args.timeout)
            data, mode_now = fetch_state(session, base, args.device, headers, args.timeout)
            if mode_now != mode:
                mode = mode_now
                if mode == "display":
                    print("source: display（状态/队列/纸量/取件码/单价 全量）")
                elif mode == "legacy":
                    print("source: legacy（无 /api/display，仅有 单价/纸量/队列，无取件码）")
                else:
                    print("source: price-only（密钥无效，只能取单价）")
            line = json.dumps(build_frame(data), ensure_ascii=False,
                              separators=(",", ":"))
            if ser is None or not ser.is_open:
                ser = open_serial(args.port, args.baud)
                print("serial reopened:", args.port)
            ser.write((line + "\n").encode("utf-8"))
            print("tx", line)
            if args.heartbeat_interval > 0 and started - last_hb >= args.heartbeat_interval:
                last_hb = started
                sheets = (data.get("paper") or {}).get("sheets")
                ok = send_heartbeat(session, base, args.device, headers,
                                    args.timeout, sheets)
                print("heartbeat %s p=%s" % ("ok" if ok else "fail", sheets))
        except Exception as exc:
            print("fetch failed:", exc)
            if isinstance(exc, (serial.SerialException, OSError)) and ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                print("serial lost, will reopen:", args.port)
        deadline = started + args.interval
        while time.time() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.time())))
            try:
                rx_buf = drain_board(ser, rx_buf, session, base, args.device,
                                     headers, args.timeout)
            except Exception as exc:
                print("pick failed:", exc)
                break


if __name__ == "__main__":
    main()
