import hashlib

import pytest
from fastapi.testclient import TestClient

import db
from main import app
from routers import display as display_router
from setting import SECRET_KEY


@pytest.fixture()
def display_client(temp_db, monkeypatch):
    db.register_device("disp-a", name="取件屏 A", paper_capacity=250)
    db.register_device("disp-b", name="取件屏 B")
    db.update_heartbeat("disp-a", status="online", paper_left=180)
    token = "disp-a-token"
    conn = db._connect()
    try:
        conn.execute(
            "INSERT INTO device_tokens (device_id, token_hash, created_at, revoked) "
            "VALUES (?, ?, 1, 0)",
            ("disp-a", hashlib.sha256(token.encode()).hexdigest()),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(display_router, "_rate_hits", {})
    return TestClient(app), token


def _headers(token):
    return {"Authorization": "Bearer " + token}


def test_state_has_full_fields_and_only_three_states(display_client):
    client, token = display_client
    response = client.get("/api/display/disp-a/state", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    for key in ("device_id", "ts", "state", "queue", "queue_len",
                "paper", "pickup", "price"):
        assert key in body
    assert body["state"] in ("idle", "busy", "pickup")
    assert body["queue"] == []
    assert body["queue_len"] == 0
    assert body["paper"] == {"level": "ok", "sheets": 180}
    assert body["price"]["bw"] == body["price"]["a4_bw"]


def test_queue_head_and_busy_state(display_client):
    client, token = display_client
    for name in ("a.pdf", "b.pdf"):
        db.add_job({"folder": "f", "filename": name, "status": "pending"},
                   printer_id="disp-a")
    body = client.get("/api/display/disp-a/state", headers=_headers(token)).json()
    assert body["state"] == "busy"
    assert body["queue_len"] == 2
    assert len(body["queue"]) == 2
    assert body["queue"][0]["title"] == "a.pdf"


def test_pickup_state_only_within_window(display_client, monkeypatch):
    client, token = display_client
    db.add_job({"folder": "f", "filename": "done.pdf", "status": "pending"},
               printer_id="disp-a")
    db.set_job_status("f", "done.pdf", "printing")
    conn = db._connect()
    try:
        conn.execute(
            "UPDATE print_jobs SET status='completed', pickup_code='5218',"
            " status_at=datetime('now','localtime') WHERE filename='done.pdf'")
        conn.commit()
    finally:
        conn.close()
    body = client.get("/api/display/disp-a/state", headers=_headers(token)).json()
    assert body["state"] == "pickup"
    assert body["pickup"]["code"] == "5218"

    monkeypatch.setattr(display_router, "PICKUP_WINDOW_SEC", -1)
    body = client.get("/api/display/disp-a/state", headers=_headers(token)).json()
    assert body["state"] == "idle"


def test_unknown_device_returns_404(display_client):
    client, _ = display_client
    response = client.get("/api/display/missing/state", headers=_headers(SECRET_KEY))
    assert response.status_code == 404


def test_auth_required_and_device_isolation(display_client):
    client, token = display_client
    assert client.get("/api/display/disp-a/state").status_code in (401, 403)
    assert client.get("/api/display/disp-b/state",
                      headers=_headers(token)).status_code == 403
    assert client.get("/api/display/disp-a/state",
                      headers=_headers(SECRET_KEY)).status_code == 200


def test_rate_limit_returns_429(display_client, monkeypatch):
    client, token = display_client
    monkeypatch.setattr(display_router, "RATE_MAX_REQUESTS", 5)
    codes = [client.get("/api/display/disp-a/state",
                        headers=_headers(token)).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5
    assert codes[-1] == 429
    assert display_router.DISPLAY_RATE_LIMIT == "10/10s"


def test_rate_limit_is_per_device(display_client, monkeypatch):
    client, token = display_client
    monkeypatch.setattr(display_router, "RATE_MAX_REQUESTS", 2)
    for _ in range(3):
        client.get("/api/display/disp-a/state", headers=_headers(token))
    assert client.get("/api/display/disp-a/state",
                      headers=_headers(token)).status_code == 429
    assert client.get("/api/display/disp-b/state",
                      headers=_headers(SECRET_KEY)).status_code == 200
