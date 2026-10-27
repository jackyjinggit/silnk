import json
import sqlite3
import urllib.error
import uuid
from unittest.mock import Mock

import numpy as np
import pytest

from client.edge import x5_eject as edge


BASE = "https://printer.example"
DEVICE = "edge-a"


def image(level, color=False):
    return np.full((12, 16, 3) if color else (12, 16), level, dtype=np.uint8)


def detector(**kwargs):
    return edge.PaperDetector((2, 2, 8, 8), stable_frames=2, clear_frames=2, **kwargs)


def feed(sensor, levels, start=0, step=1):
    return [sensor.update(image(level), start + i * step) for i, level in enumerate(levels)]


def test_requires_clear_baseline_and_does_not_report_stationary_paper():
    sensor = detector()
    assert feed(sensor, [240] * 20) == [None] * 20
    assert feed(sensor, [20, 20, 240, 240, 240], start=20) == [None] * 4 + [1.0]
    assert feed(sensor, [240] * 20, start=25) == [None] * 20


def test_stability_debounce_resets_on_motion_and_short_clear_does_not_rearm():
    sensor = detector()
    assert feed(sensor, [20, 20, 240, 240, 200, 240, 240, 240]) == [None] * 7 + [1.0]
    assert feed(sensor, [20, 240, 240, 240], start=8) == [None] * 4
    assert feed(sensor, [20, 20, 240, 240, 240], start=12) == [None] * 4 + [1.0]


def test_cooldown_suppresses_event_without_delayed_static_report():
    sensor = detector(cooldown=10)
    assert feed(sensor, [20, 20, 240, 240, 240])[-1] == 1.0
    assert feed(sensor, [20, 20, 240, 240, 240], start=5) == [None] * 5
    assert feed(sensor, [240] * 10, start=10) == [None] * 10
    assert feed(sensor, [20, 20, 240, 240, 240], start=20)[-1] == 1.0


def test_roi_ignores_outside_motion_and_brightness_without_entry_motion():
    sensor = detector(motion_threshold=100)
    assert feed(sensor, [20, 20]) == [None, None]
    for tick, level in enumerate([60, 100, 140, 180, 220, 240, 240, 240], start=2):
        frame = image(255, color=True)
        frame[2:10, 2:10] = level
        assert sensor.update(frame, tick) is None
    sensor = detector()
    assert feed(sensor, [20, 20]) == [None, None]
    for tick in range(2, 8):
        frame = image(240, color=True)
        frame[2:10, 2:10] = 20
        assert sensor.update(frame, tick) is None


def test_paper_coverage_and_clear_hysteresis():
    sensor = detector()
    feed(sensor, [20, 20])
    partial = image(20)
    partial[2:6, 2:10] = 240
    for tick in range(2, 6):
        assert sensor.update(partial, tick) is None
    assert sensor.update(image(240), 6) is None
    assert sensor.update(image(240), 7) is None
    assert sensor.update(image(240), 8) == 1.0


@pytest.mark.parametrize("kwargs", [
    {"roi": (-1, 0, 10, 10)}, {"roi": (0, 0, 0, 10)},
    {"stable_frames": 0}, {"clear_frames": 1.5}, {"cooldown": -1},
    {"cooldown": float("nan")}, {"brightness": 256},
    {"paper_ratio": 0.2, "clear_ratio": 0.2},
    {"motion_threshold": 3, "stable_threshold": 3},
])
def test_invalid_detector_parameters(kwargs):
    with pytest.raises(ValueError):
        edge.PaperDetector(**{"roi": (0, 0, 8, 8), **kwargs})


def test_invalid_frames_and_clock():
    sensor = detector()
    for frame in (None, image(20).astype(float), np.zeros((2, 2), dtype=np.uint8),
                  np.zeros((12, 16, 2), dtype=np.uint8)):
        with pytest.raises(ValueError):
            sensor.update(frame, 1)
    sensor.update(image(20), 2)
    with pytest.raises(ValueError):
        sensor.update(image(20), 1)


def baseline_detector(**kwargs):
    return edge.BaselineDetector((2, 2, 8, 8), stable_frames=2, clear_frames=2,
                                 baseline_frames=3, **kwargs)


def test_baseline_detects_dark_paper_once_and_stays_quiet_while_present():
    sensor = baseline_detector()
    assert feed(sensor, [40, 40, 40]) == [None, None, None]
    assert feed(sensor, [40, 40, 40], start=3) == [None, None, None]
    assert feed(sensor, [120, 120], start=6) == [None, 1.0]
    assert feed(sensor, [120] * 10, start=8) == [None] * 10


def test_baseline_rearms_after_paper_removed():
    sensor = baseline_detector(cooldown=0)
    assert feed(sensor, [40, 40, 40]) == [None, None, None]
    assert feed(sensor, [120, 120], start=3) == [None, 1.0]
    assert feed(sensor, [40, 40, 40, 120, 120], start=5) == [None, None, None, None, 1.0]


def test_baseline_ignores_static_scene_and_partial_change():
    sensor = baseline_detector()
    feed(sensor, [40, 40, 40])
    assert feed(sensor, [40] * 30, start=3) == [None] * 30
    frame = image(40)
    frame[2:4, 2:10] = 120
    assert [sensor.update(frame, 40 + i) for i in range(5)] == [None] * 5


@pytest.mark.parametrize("kwargs", [
    {"pixel_threshold": 0}, {"baseline_frames": 0}, {"clear_frames": 0},
    {"paper_ratio": 0.2, "clear_ratio": 0.2}, {"cooldown": -1},
])
def test_invalid_baseline_detector_parameters(kwargs):
    with pytest.raises(ValueError):
        edge.BaselineDetector(**{"roi": (0, 0, 8, 8), **kwargs})


def queue_rows(queue):
    with sqlite3.connect(queue.path) as conn:
        return conn.execute(
            "SELECT payload, attempts, next_attempt FROM eject_outbox ORDER BY rowid").fetchall()


def test_queue_survives_restart_and_retries_exact_uuid_and_payload(tmp_path):
    path = tmp_path / "outbox.db"
    queue = edge.EventQueue(path)
    payload = queue.enqueue(BASE, DEVICE, "vision", 0.85, ts=123)
    assert str(uuid.UUID(payload["event_id"])) == payload["event_id"]
    calls = []

    def timeout_after_accepting(backend, device, token, event, timeout):
        calls.append((backend, device, token, event, timeout))
        raise urllib.error.URLError("timeout after server commit")

    assert queue.flush(BASE, DEVICE, "token", now=100, sender=timeout_after_accepting) == 0
    rows = queue_rows(queue)
    assert rows == [(json.dumps(payload, separators=(",", ":")), 1, 102.0)]
    queue = edge.EventQueue(path)
    sender = Mock(return_value=True)
    assert queue.flush(BASE, DEVICE, "token", now=101, sender=sender) == 0
    sender.assert_not_called()
    assert queue.flush(BASE, DEVICE, "token", now=102, sender=sender) == 1
    assert sender.call_args.args == calls[0]
    assert queue.pending_count() == 0


def test_queue_backoff_cap_and_target_isolation(tmp_path):
    queue = edge.EventQueue(tmp_path / "queue.db")
    first = queue.enqueue(BASE, DEVICE, "manual", ts=1)
    queue.enqueue(BASE, "other-device", "manual", ts=1)
    queue.enqueue("https://other.example", DEVICE, "manual", ts=1)
    sender = Mock(return_value=False)
    for now, next_attempt in [(0, 2), (2, 6), (6, 11), (11, 16)]:
        assert queue.flush(BASE, DEVICE, "token", retry_max=5, now=now, sender=sender) == 0
        assert queue_rows(queue)[0][2] == next_attempt
    assert all(call.args[3] == first for call in sender.call_args_list)
    assert queue.pending_count() == 3
    assert queue.flush(BASE, DEVICE, "token", now=16, sender=Mock(return_value=True)) == 1
    assert queue.pending_count() == 2


def test_failed_event_does_not_block_other_due_events(tmp_path):
    queue = edge.EventQueue(tmp_path / "queue.db")
    queue.enqueue(BASE, DEVICE, "manual", ts=1)
    second = queue.enqueue(BASE, DEVICE, "manual", ts=2)
    sender = Mock(side_effect=[False, True])
    assert queue.flush(BASE, DEVICE, "token", now=10, sender=sender) == 1
    assert queue.pending_count() == 1
    assert sender.call_args.args[3] == second


@pytest.mark.parametrize("source,confidence,ts", [
    ("other", None, 1), ("manual", 0.8, 1), ("vision", None, 1),
    ("vision", float("nan"), 1), ("vision", 1.1, 1), ("manual", None, 0),
    ("manual", None, 1.5),
])
def test_invalid_events_never_enter_queue(tmp_path, source, confidence, ts):
    queue = edge.EventQueue(tmp_path / "queue.db")
    with pytest.raises(ValueError):
        queue.enqueue(BASE, DEVICE, source, confidence, ts)
    assert queue.pending_count() == 0


def test_manual_mode_is_explicit_and_does_not_open_camera(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PRINTER_KEY", "device-token")
    monkeypatch.setattr(edge, "activity_open", Mock(return_value=False))
    sender = Mock(return_value=False)
    monkeypatch.setattr(edge, "send_event", sender)
    path = tmp_path / "queue.db"
    assert edge.main(["--manual", "--device", DEVICE, "--queue", str(path)]) == 0
    sender.assert_not_called()
    assert "未上报" in capsys.readouterr().out
    assert edge.EventQueue(path).pending_count() == 0


@pytest.mark.parametrize("argv", [
    ["--interval", "0"], ["--timeout", "nan"], ["--cooldown", "inf"],
    ["--stable-frames", "0"], ["--roi", "1,2,3"], ["--roi", "-1,0,2,2"],
    ["--retry-delay", "5", "--retry-max", "2"], ["--queue", ":memory:"],
    ["--backend", "https://user:password@example.com"], ["--backend", "ftp://example.com"],
    ["--backend", "https://example.com?key=secret"], ["--camera", "-1"],
])
def test_cli_rejects_invalid_parameters(argv, monkeypatch):
    monkeypatch.setenv("PRINTER_KEY", "token")
    with pytest.raises(SystemExit) as exc:
        edge.parse_args(["--device", DEVICE] + argv)
    assert exc.value.code == 2


def test_environment_conventions_and_missing_token(monkeypatch):
    monkeypatch.delenv("PRINTER_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "legacy-token")
    monkeypatch.setenv("PRINTER_DEVICE_ID", DEVICE)
    monkeypatch.setenv("SERVER_HOST", BASE + "/")
    args = edge.parse_args([])
    assert (args.token, args.device, args.backend) == ("legacy-token", DEVICE, BASE)
    monkeypatch.setenv("PRINTER_KEY", "device-token")
    assert edge.parse_args([]).token == "device-token"
    monkeypatch.delenv("PRINTER_KEY")
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(SystemExit):
        edge.parse_args([])


def mock_response(body, status=200):
    response = Mock(status=status)
    response.read.return_value = json.dumps(body).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def test_http_bearer_encoded_device_timeout_and_matching_ack(tmp_path, monkeypatch):
    queue = edge.EventQueue(tmp_path / "queue.db")
    payload = queue.enqueue(BASE, DEVICE, "vision", 0.9, ts=1)
    opener = Mock()
    opener.open.return_value = mock_response({
        "message": "success", "data": {"event": {**payload, "device_id": DEVICE}}})
    monkeypatch.setattr(edge.urllib.request, "build_opener", Mock(return_value=opener))
    assert edge.send_event(BASE, "a/b", "private-token", payload, 4)
    request = opener.open.call_args.args[0]
    assert request.full_url == BASE + "/api/edge/a%2Fb/eject"
    assert request.get_header("Authorization") == "Bearer private-token"
    assert json.loads(request.data) == payload
    assert opener.open.call_args.kwargs == {"timeout": 4}
    assert edge.NoRedirect().redirect_request(request, None, 302, "", {}, BASE) is None


@pytest.mark.parametrize("body,status", [
    ({"message": "success", "data": {"event": {"event_id": "wrong"}}}, 200),
    ({"message": "success"}, 200), ([], 200),
    ({"message": "success", "data": {"event": {}}}, 202),
])
def test_bad_ack_keeps_event_for_retry(tmp_path, monkeypatch, body, status):
    queue = edge.EventQueue(tmp_path / "queue.db")
    queue.enqueue(BASE, DEVICE, "manual", ts=1)
    opener = Mock()
    opener.open.return_value = mock_response(body, status)
    monkeypatch.setattr(edge.urllib.request, "build_opener", Mock(return_value=opener))
    assert queue.flush(BASE, DEVICE, "token", now=1) == 0
    assert queue.pending_count() == 1


@pytest.mark.parametrize("status", [401, 403, 409, 422, 429, 500])
def test_http_errors_preserve_event(tmp_path, monkeypatch, status):
    queue = edge.EventQueue(tmp_path / "queue.db")
    queue.enqueue(BASE, DEVICE, "manual", ts=1)
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError(BASE, status, "failure", {}, None)
    monkeypatch.setattr(edge.urllib.request, "build_opener", Mock(return_value=opener))
    assert queue.flush(BASE, DEVICE, "token", now=1) == 0
    assert queue_rows(queue)[0][1:] == (1, 3.0)


def test_vision_main_releases_camera_and_persists_event(tmp_path, monkeypatch):
    import cv2

    monkeypatch.setenv("PRINTER_KEY", "token")
    capture = Mock()
    capture.isOpened.return_value = True
    capture.read.side_effect = [(True, image(v)) for v in [20, 20, 240, 240, 240]] + [KeyboardInterrupt()]
    monkeypatch.setattr(cv2, "VideoCapture", Mock(return_value=capture))
    monkeypatch.setattr(edge, "retry_loop", lambda *args: None)
    monkeypatch.setattr(edge, "activity_open", Mock(return_value=True))
    monkeypatch.setattr(edge.time, "sleep", lambda interval: None)
    path = tmp_path / "queue.db"
    assert edge.main(["--device", DEVICE, "--queue", str(path), "--roi", "2,2,8,8",
                      "--clear-frames", "2", "--stable-frames", "2"]) == 0
    capture.release.assert_called_once()
    payload = json.loads(queue_rows(edge.EventQueue(path))[0][0])
    assert payload["source"] == "vision"
    assert payload["confidence"] == 1.0


def test_retry_after_lost_response_creates_one_backend_event(tmp_path, temp_db):
    from fastapi.testclient import TestClient

    import db
    from main import app
    from setting import SECRET_KEY

    db.register_device(DEVICE, name="edge")
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id=DEVICE)
    db.set_job_status("f", "a.pdf", "printing")
    client = TestClient(app)
    path = tmp_path / "queue.db"
    queue = edge.EventQueue(path)
    payload = queue.enqueue(BASE, DEVICE, "vision", 0.8, ts=123)
    calls = []

    def deliver(backend, device, token, event, timeout):
        response = client.post("/api/edge/%s/eject" % device, json=event,
                               headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        calls.append(response.json())
        if len(calls) == 1:
            raise urllib.error.URLError("response lost")
        return response.json()["data"]["event"]["event_id"] == event["event_id"]

    assert queue.flush(BASE, DEVICE, SECRET_KEY, now=0, sender=deliver) == 0
    assert len(db.list_eject_events(DEVICE)) == 1
    queue = edge.EventQueue(path)
    assert queue.flush(BASE, DEVICE, SECRET_KEY, now=2, sender=deliver) == 1
    assert calls[0] == calls[1]
    events = db.list_eject_events(DEVICE)
    assert len(events) == 1
    assert events[0]["event_id"] == payload["event_id"]
    assert queue.pending_count() == 0
