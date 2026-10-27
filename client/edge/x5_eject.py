import argparse
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from collections import deque
from pathlib import Path
from urllib.parse import quote, urlsplit


def finite_number(value, name, minimum=0, maximum=None, inclusive=True):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < minimum
            or (not inclusive and value == minimum)
            or (maximum is not None and value > maximum)):
        raise ValueError("%s 超出有效范围" % name)
    return value


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s 必须为正整数" % name)
    return value


def validate_roi(roi):
    if (len(roi) != 4 or any(isinstance(v, bool) or not isinstance(v, int) for v in roi)
            or min(roi[:2]) < 0 or min(roi[2:]) < 1):
        raise ValueError("ROI 必须为 x,y,width,height，坐标非负且宽高为正")
    return tuple(roi)


def normalize_backend(backend):
    parsed = urlsplit(backend)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(c.isspace() for c in backend)):
        raise ValueError("backend 必须为无凭据、查询串或片段的 HTTP(S) 地址")
    parsed.port
    return backend.rstrip("/")


class PaperDetector:
    def __init__(self, roi, brightness=180.0, paper_ratio=0.7,
                 clear_ratio=0.2, motion_threshold=12.0, stable_threshold=3.0,
                 stable_frames=3, clear_frames=3, cooldown=2.0):
        self.roi = validate_roi(roi)
        self.brightness = finite_number(brightness, "brightness", 0, 255, False)
        self.paper_ratio = finite_number(paper_ratio, "paper_ratio", 0, 1, False)
        self.clear_ratio = finite_number(clear_ratio, "clear_ratio", 0, 1)
        if clear_ratio >= paper_ratio:
            raise ValueError("clear_ratio 必须小于 paper_ratio")
        self.motion_threshold = finite_number(motion_threshold, "motion_threshold", 0, 255, False)
        self.stable_threshold = finite_number(stable_threshold, "stable_threshold", 0, 255)
        if stable_threshold >= motion_threshold:
            raise ValueError("stable_threshold 必须小于 motion_threshold")
        self.stable_frames = positive_int(stable_frames, "stable_frames")
        self.clear_frames = positive_int(clear_frames, "clear_frames")
        self.cooldown = finite_number(cooldown, "cooldown")
        self.previous = None
        self.armed = False
        self.candidate = False
        self.clear_count = 0
        self.stable_count = 0
        self.last_event = -math.inf
        self.last_now = -math.inf

    def update(self, frame, now=None):
        import cv2

        now = time.monotonic() if now is None else now
        finite_number(now, "now")
        if now < self.last_now:
            raise ValueError("检测时钟必须单调递增")
        self.last_now = now
        if frame is None or frame.size == 0 or frame.dtype.name != "uint8":
            raise ValueError("frame 必须为非空 uint8 图像")
        x, y, width, height = self.roi
        if frame.ndim not in (2, 3) or y + height > frame.shape[0] or x + width > frame.shape[1]:
            raise ValueError("ROI 超出图像范围")
        crop = frame[y:y + height, x:x + width]
        if crop.ndim == 3:
            if crop.shape[2] not in (3, 4):
                raise ValueError("frame 必须为灰度、BGR 或 BGRA 图像")
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY if crop.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
        ratio = cv2.countNonZero(cv2.threshold(crop, self.brightness - 1, 255, cv2.THRESH_BINARY)[1]) / crop.size
        if self.previous is not None and crop.shape != self.previous.shape:
            raise ValueError("ROI 图像尺寸发生变化")
        difference = 0.0 if self.previous is None else cv2.mean(cv2.absdiff(crop, self.previous))[0]
        self.previous = crop.copy()
        if ratio <= self.clear_ratio:
            self.clear_count += 1
            self.stable_count = 0
            self.candidate = False
            if self.clear_count >= self.clear_frames:
                self.armed = True
            return None
        self.clear_count = 0
        if not self.armed:
            return None
        if not self.candidate and difference >= self.motion_threshold:
            self.candidate = True
        if ratio < self.paper_ratio:
            self.stable_count = 0
            return None
        if self.candidate and difference <= self.stable_threshold:
            self.stable_count += 1
        else:
            self.stable_count = 0
        if self.stable_count < self.stable_frames:
            return None
        self.armed = False
        self.candidate = False
        self.stable_count = 0
        if now - self.last_event < self.cooldown:
            return None
        self.last_event = now
        return ratio


class BaselineDetector:
    def __init__(self, roi, pixel_threshold=25.0, paper_ratio=0.35, clear_ratio=0.15,
                 stable_frames=3, clear_frames=3, cooldown=2.0, baseline_frames=20,
                 window_size=10, window_hits=None):
        self.roi = validate_roi(roi)
        self.pixel_threshold = finite_number(pixel_threshold, "pixel_threshold", 0, 255, False)
        self.paper_ratio = finite_number(paper_ratio, "paper_ratio", 0, 1, False)
        self.clear_ratio = finite_number(clear_ratio, "clear_ratio", 0, 1)
        if clear_ratio >= paper_ratio:
            raise ValueError("clear_ratio 必须小于 paper_ratio")
        self.stable_frames = positive_int(stable_frames, "stable_frames")
        self.clear_frames = positive_int(clear_frames, "clear_frames")
        self.cooldown = finite_number(cooldown, "cooldown")
        self.baseline_frames = positive_int(baseline_frames, "baseline_frames")
        self.window_size = positive_int(window_size, "window_size")
        hits = self.stable_frames if window_hits is None else window_hits
        self.window_hits = positive_int(hits, "window_hits")
        if self.window_hits > self.window_size:
            raise ValueError("window_hits 不能大于 window_size")
        self.window = deque(maxlen=self.window_size)
        self.baseline = None
        self.samples = []
        self.armed = False
        self.present = False
        self.clear_count = 0
        self.last_event = -math.inf
        self.last_now = -math.inf
        self.shape = None
        self.last_ratio = 0.0

    def _crop(self, frame):
        import cv2

        x, y, width, height = self.roi
        if frame.ndim not in (2, 3) or y + height > frame.shape[0] or x + width > frame.shape[1]:
            raise ValueError("ROI 超出图像范围")
        crop = frame[y:y + height, x:x + width]
        if crop.ndim == 3:
            if crop.shape[2] not in (3, 4):
                raise ValueError("frame 必须为灰度、BGR 或 BGRA 图像")
            crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY if crop.shape[2] == 3 else cv2.COLOR_BGRA2GRAY)
        if self.shape is None:
            self.shape = crop.shape
        elif crop.shape != self.shape:
            raise ValueError("ROI 图像尺寸发生变化")
        return crop

    def update(self, frame, now=None):
        import cv2
        import numpy as np

        now = time.monotonic() if now is None else now
        finite_number(now, "now")
        if now < self.last_now:
            raise ValueError("检测时钟必须单调递增")
        self.last_now = now
        if frame is None or frame.size == 0 or frame.dtype.name != "uint8":
            raise ValueError("frame 必须为非空 uint8 图像")
        crop = self._crop(frame)
        if self.baseline is None:
            self.samples.append(crop.astype(np.float32))
            if len(self.samples) < self.baseline_frames:
                return None
            self.baseline = np.median(np.stack(self.samples), axis=0).astype(np.uint8)
            self.samples = []
            self.armed = True
            return None
        difference = cv2.absdiff(crop, self.baseline)
        ratio = float((difference > self.pixel_threshold).mean())
        self.last_ratio = ratio
        if ratio <= self.clear_ratio:
            self.clear_count += 1
            self.window.clear()
            if self.clear_count >= self.clear_frames:
                self.armed = True
                self.present = False
            return None
        self.clear_count = 0
        if self.present or not self.armed:
            return None
        self.window.append(ratio >= self.paper_ratio)
        if sum(self.window) < self.window_hits:
            return None
        self.present = True
        self.armed = False
        self.window.clear()
        if now - self.last_event < self.cooldown:
            return None
        self.last_event = now
        return min(1.0, max(ratio, self.paper_ratio))


class EventQueue:
    def __init__(self, path):
        if str(path) == ":memory:":
            raise ValueError("队列必须使用持久 SQLite 文件")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS eject_outbox ("
                "event_id TEXT PRIMARY KEY, backend TEXT NOT NULL, device TEXT NOT NULL, "
                "payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                "next_attempt REAL NOT NULL DEFAULT 0)")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def enqueue(self, backend, device, source, confidence=None, ts=None, photo=None):
        backend = normalize_backend(backend)
        if not isinstance(device, str) or not device.strip():
            raise ValueError("device 不能为空")
        if source not in ("manual", "vision"):
            raise ValueError("source 必须为 manual 或 vision")
        if source == "manual" and confidence is not None:
            raise ValueError("manual 不允许携带视觉置信度")
        if source == "vision":
            finite_number(confidence, "confidence", 0, 1)
        if photo is not None and not isinstance(photo, str):
            raise ValueError("photo 必须为 base64 字符串")
        ts = int(time.time()) if ts is None else ts
        positive_int(ts, "ts")
        payload = {"event_id": str(uuid.uuid4()), "event": "sheet_out",
                   "source": source, "ts": ts, "confidence": confidence}
        if photo is not None:
            payload["photo"] = photo
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO eject_outbox (event_id, backend, device, payload) VALUES (?, ?, ?, ?)",
                (payload["event_id"], backend, device, json.dumps(payload, separators=(",", ":"))))
        return payload

    def pending_count(self):
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM eject_outbox").fetchone()[0]

    def flush(self, backend, device, token, timeout=6.0, retry_delay=2.0,
              retry_max=300.0, limit=20, now=None, sender=None):
        backend = normalize_backend(backend)
        validate_token(token)
        finite_number(timeout, "timeout", inclusive=False)
        finite_number(retry_delay, "retry_delay", inclusive=False)
        finite_number(retry_max, "retry_max", retry_delay)
        positive_int(limit, "limit")
        now = time.time() if now is None else now
        finite_number(now, "now")
        sender = send_event if sender is None else sender
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT event_id, payload, attempts FROM eject_outbox "
                "WHERE backend=? AND device=? AND next_attempt<=? ORDER BY rowid LIMIT ?",
                (backend, device, now, limit)).fetchall()
        sent = 0
        for event_id, raw, attempts in rows:
            payload = json.loads(raw)
            try:
                acknowledged = sender(backend, device, token, payload, timeout)
            except (OSError, ValueError, urllib.error.URLError):
                acknowledged = False
            with self.connect() as conn:
                if acknowledged:
                    conn.execute("DELETE FROM eject_outbox WHERE event_id=?", (event_id,))
                    sent += 1
                else:
                    delay = min(retry_max, retry_delay * (2 ** min(attempts, 30)))
                    conn.execute(
                        "UPDATE eject_outbox SET attempts=attempts+1, next_attempt=? WHERE event_id=?",
                        (now + delay, event_id))
        return sent


def validate_token(token):
    if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
        raise ValueError("缺少有效环境变量 PRINTER_KEY / SECRET_KEY")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_event(backend, device, token, payload, timeout):
    request = urllib.request.Request(
        "%s/api/edge/%s/eject" % (backend, quote(device, safe="")),
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Accept": "application/json"}, method="POST")
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if response.status != 200:
            return False
        body = json.loads(response.read())
    if not isinstance(body, dict) or body.get("message") != "success":
        return False
    data = body.get("data")
    event = data.get("event") if isinstance(data, dict) else None
    if not isinstance(event, dict):
        return False
    keys = ("event_id", "event", "source", "ts", "confidence")
    return all(event.get(k) == payload.get(k) for k in keys)


def activity_open(backend, device, token, timeout):
    request = urllib.request.Request(
        "%s/api/edge/%s/activity" % (backend, quote(device, safe="")),
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        method="GET")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False
    data = body.get("data") if isinstance(body, dict) else None
    return body.get("message") == "success" and isinstance(data, dict) and data.get("active") is True


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=os.getenv("PRINTER_DEVICE_ID", ""))
    parser.add_argument("--backend", default=os.getenv("SERVER_HOST") or "http://127.0.0.1:8000")
    parser.add_argument("--queue", default=str(Path(__file__).with_name("x5_eject.sqlite3")))
    parser.add_argument("--manual", action="store_true", help="手动上报一次出纸，source=manual")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--snapshot-dir", default="")
    parser.add_argument("--peak-dir", default="")
    parser.add_argument("--log-interval", type=float, default=0.0)
    parser.add_argument("--mode", choices=("bright", "baseline"), default="bright",
                        help="bright=亮纸检测，baseline=与空托盘基准帧对比")
    parser.add_argument("--pixel-threshold", type=float, default=25.0)
    parser.add_argument("--baseline-frames", type=int, default=20)
    parser.add_argument("--roi", default="0,0,320,240")
    parser.add_argument("--brightness", type=float, default=180.0)
    parser.add_argument("--paper-ratio", type=float, default=0.7)
    parser.add_argument("--clear-ratio", type=float, default=0.2)
    parser.add_argument("--motion-threshold", type=float, default=12.0)
    parser.add_argument("--stable-threshold", type=float, default=3.0)
    parser.add_argument("--stable-frames", type=int, default=3)
    parser.add_argument("--window-size", type=int, default=10)
    parser.add_argument("--window-hits", type=int, default=None)
    parser.add_argument("--clear-frames", type=int, default=3)
    parser.add_argument("--cooldown", type=float, default=2.0)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--retry-max", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        if not args.device.strip():
            raise ValueError("缺少 --device 或 PRINTER_DEVICE_ID")
        args.backend = normalize_backend(args.backend)
        args.token = os.getenv("PRINTER_KEY") or os.getenv("SECRET_KEY") or ""
        validate_token(args.token)
        args.roi = validate_roi(tuple(int(v) for v in args.roi.split(",")))
        if args.mode == "baseline":
            args.detector = BaselineDetector(
                args.roi, args.pixel_threshold, args.paper_ratio, args.clear_ratio,
                args.stable_frames, args.clear_frames, args.cooldown, args.baseline_frames,
                args.window_size, args.window_hits)
        else:
            args.detector = PaperDetector(
                args.roi, args.brightness, args.paper_ratio, args.clear_ratio,
                args.motion_threshold, args.stable_threshold, args.stable_frames,
                args.clear_frames, args.cooldown)
        if args.camera < 0:
            raise ValueError("camera 必须非负")
        for name in ("width", "height"):
            positive_int(getattr(args, name), name)
        if not args.queue.strip() or args.queue == ":memory:":
            raise ValueError("queue 必须为持久 SQLite 文件路径")
        for name in ("interval", "timeout", "retry_delay"):
            finite_number(getattr(args, name), name, inclusive=False)
        finite_number(args.log_interval, "log_interval")
        finite_number(args.retry_max, "retry_max", args.retry_delay)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def retry_loop(queue, args, stop):
    while not stop.is_set():
        queue.flush(args.backend, args.device, args.token, args.timeout,
                    args.retry_delay, args.retry_max)
        stop.wait(min(args.retry_delay, 1.0))


def encode_photo(frame, quality=80):
    """把现场帧压成 JPEG 再转 base64，作为事件证据随上报一起发出去。"""
    import base64

    import cv2

    ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return None
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def save_snapshot(directory, frame, event_id, confidence):
    import cv2

    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    label = "sheet_out %.2f" % confidence
    cv2.putText(frame, label, (24, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)
    cv2.putText(frame, event_id, (24, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    path = target / ("%s.jpg" % event_id)
    cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return path


def main(argv=None):
    args = parse_args(argv)
    queue = EventQueue(args.queue)
    if args.manual:
        if not activity_open(args.backend, args.device, args.token, args.timeout):
            print("当前没有活动订单，未上报出纸事件")
            return 0
        payload = queue.enqueue(args.backend, args.device, "manual")
        print("manual 手动出纸事件已持久化: %s" % payload["event_id"])
        queue.flush(args.backend, args.device, args.token, args.timeout,
                    args.retry_delay, args.retry_max)
        pending = queue.pending_count()
        print("待重试事件: %d" % pending)
        return 1 if pending else 0
    import cv2

    capture = cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
    capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    stop = threading.Event()
    worker = threading.Thread(target=retry_loop, args=(queue, args, stop), daemon=True)
    try:
        if not capture.isOpened():
            raise RuntimeError("摄像头无法打开")
        worker.start()
        last_log = 0.0
        last_peak = 0.0
        while True:
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError("摄像头读取失败")
            confidence = args.detector.update(frame)
            if args.log_interval > 0 and time.monotonic() - last_log >= args.log_interval:
                last_log = time.monotonic()
                print("ratio %.3f present=%s" % (getattr(args.detector, "last_ratio", 0.0),
                                                 getattr(args.detector, "present", False)))
            if (args.peak_dir and confidence is None
                    and getattr(args.detector, "last_ratio", 0.0) >= 0.3
                    and time.monotonic() - last_peak >= 2.0):
                last_peak = time.monotonic()
                target = Path(args.peak_dir)
                target.mkdir(parents=True, exist_ok=True)
                name = "peak-%d.jpg" % int(time.time())
                cv2.imwrite(str(target / name), frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                print("peak: %s ratio=%.3f" % (name, args.detector.last_ratio))
            if confidence is not None and activity_open(
                    args.backend, args.device, args.token, args.timeout):
                photo = encode_photo(frame)
                payload = queue.enqueue(args.backend, args.device, "vision", confidence,
                                        photo=photo)
                print("vision 出纸事件已持久化: %s 现场图=%s"
                      % (payload["event_id"], "有" if photo else "无"))
                if args.snapshot_dir:
                    path = save_snapshot(args.snapshot_dir, frame.copy(),
                                         payload["event_id"], confidence)
                    print("snapshot: %s" % path)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        if worker.is_alive():
            worker.join(timeout=args.timeout + 1)
        capture.release()


if __name__ == "__main__":
    raise SystemExit(main())
