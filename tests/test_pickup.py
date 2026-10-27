import hashlib

import pytest
from fastapi.testclient import TestClient

import alerts
import db
from main import app
from routers import display as display_router
from setting import SECRET_KEY


@pytest.fixture()
def pickup_client(temp_db, monkeypatch):
    db.register_device("pk-a", name="取件屏 A", paper_capacity=250)
    db.register_device("pk-b", name="取件屏 B")
    db.update_heartbeat("pk-a", status="online", paper_left=180)
    db.update_heartbeat("pk-b", status="online", paper_left=180)
    token = "pk-a-token"
    conn = db._connect()
    try:
        conn.execute(
            "INSERT INTO device_tokens (device_id, token_hash, created_at, revoked) "
            "VALUES (?, ?, 1, 0)",
            ("pk-a", hashlib.sha256(token.encode()).hexdigest()),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(display_router, "_rate_hits", {})
    monkeypatch.setattr(alerts.notify, "push_alert", lambda alert: None)
    return TestClient(app), token


def _headers(token):
    return {"Authorization": "Bearer " + token}


def _completed(name, code, device="pk-a"):
    job_id = db.add_job({"folder": "f", "filename": name, "status": "pending",
                         "pickup_code": code, "out_trade_no": "T" + name},
                        printer_id=device)
    db.set_job_status("f", name, "printing")
    db.complete_job(job_id=job_id)
    return job_id


def _pick(client, token, code, device="pk-a"):
    return client.post("/api/display/%s/pickup" % device,
                       json={"code": code}, headers=_headers(token))


def test_pick_marks_order_and_writes_seq(pickup_client):
    client, token = pickup_client
    job_id = _completed("a.pdf", "5218")
    response = _pick(client, token, "5218")
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == job_id
    assert body["duplicate"] is False
    assert body["pickup_seq"] == 1
    assert body["skipped"] == 0
    job = db.get_job_by_id(job_id)
    assert job["status"] == "completed"
    assert job["picked_at"]
    assert job["pickup_seq"] == 1


def test_pick_is_idempotent(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "5218")
    first = _pick(client, token, "5218").json()
    second = _pick(client, token, "5218")
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["pickup_seq"] == first["pickup_seq"]
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM print_jobs WHERE picked_at IS NOT NULL").fetchone()
    finally:
        conn.close()
    assert rows["n"] == 1


def test_unknown_code_404_and_bad_code_400(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "5218")
    assert _pick(client, token, "0000").status_code == 404
    assert _pick(client, token, "   ").status_code == 400


def test_pick_requires_matching_device_and_auth(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "5218")
    assert client.post("/api/display/pk-a/pickup", json={"code": "5218"}).status_code in (401, 403)
    assert _pick(client, token, "5218", device="pk-b").status_code == 403
    assert client.post("/api/display/pk-a/pickup", json={"code": "5218"},
                       headers=_headers(SECRET_KEY)).status_code == 200


def test_code_of_other_device_not_found(pickup_client):
    client, token = pickup_client
    _completed("b.pdf", "7777", device="pk-b")
    assert _pick(client, token, "7777").status_code == 404


def test_state_returns_idle_within_next_poll_after_pick(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "5218")
    state = client.get("/api/display/pk-a/state", headers=_headers(token)).json()
    assert state["state"] == "pickup"
    assert state["pickup"]["code"] == "5218"
    _pick(client, token, "5218")
    state = client.get("/api/display/pk-a/state", headers=_headers(token)).json()
    assert state["state"] == "idle"
    assert state["pickup"]["code"] == ""


def test_out_of_order_pick_records_skipped_and_raises_r5(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "1111")
    second = _completed("b.pdf", "2222")
    body = _pick(client, token, "2222").json()
    assert body["order_id"] == second
    assert body["skipped"] == 1
    created = alerts.evaluate()
    r5 = [a for a in created if a["rule"] == "R5" and a["device_id"] == "pk-a"]
    assert len(r5) == 1
    stored = [a for a in db.list_alerts(resolved=False) if a["rule"] == "R5"]
    assert len(stored) == 1


def test_in_order_pick_does_not_raise_r5(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "1111")
    _completed("b.pdf", "2222")
    assert _pick(client, token, "1111").json()["skipped"] == 0
    assert _pick(client, token, "2222").json()["skipped"] == 0
    assert _pick(client, token, "2222").json()["duplicate"] is True
    created = alerts.evaluate()
    assert [a for a in created if a["rule"] == "R5"] == []


def test_pick_seq_increments_per_device(pickup_client):
    client, token = pickup_client
    _completed("a.pdf", "1111")
    _completed("b.pdf", "2222")
    assert _pick(client, token, "1111").json()["pickup_seq"] == 1
    assert _pick(client, token, "2222").json()["pickup_seq"] == 2
