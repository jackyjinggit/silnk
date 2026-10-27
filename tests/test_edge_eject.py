import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

import db
from main import app
from setting import SECRET_KEY
from jwt import create_user_token


@pytest.fixture()
def edge_client(temp_db, monkeypatch):
    db.register_device("edge-a", name="设备 A")
    db.register_device("edge-b", name="设备 B")
    token = "edge-a-token"
    conn = db._connect()
    try:
        conn.execute(
            "INSERT INTO device_tokens (device_id, token_hash, created_at, revoked) "
            "VALUES (?, ?, 1, 0)",
            ("edge-a", hashlib.sha256(token.encode()).hexdigest()),
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(db, "DB_PATH", db.DB_PATH)
    return TestClient(app), token


def _headers(token):
    return {"Authorization": "Bearer " + token}


def test_post_eject_is_idempotent_and_get_filters_events(edge_client):
    client, token = edge_client
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id="edge-a")
    db.set_job_status("f", "a.pdf", "printing")
    payload = {
        "event_id": "evt-001",
        "event": "sheet_out",
        "source": "vision",
        "ts": 1690000000,
        "confidence": 0.82,
    }
    first = client.post("/api/edge/edge-a/eject", json=payload,
                        headers=_headers(token))
    retry = client.post("/api/edge/edge-a/eject", json=payload,
                        headers=_headers(token))
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()

    manual = {**payload, "event_id": "evt-002", "source": "manual", "ts": 1690000001,
              "confidence": None}
    assert client.post("/api/edge/edge-a/eject", json=manual,
                       headers=_headers(token)).status_code == 200

    listed = client.get("/api/edge/edge-a/eject?since=1690000001&limit=1",
                        headers=_headers(token))
    events = listed.json()["data"]["events"]
    assert [item["event_id"] for item in events] == ["evt-002"]
    assert events[0]["source"] == "manual"
    assert len(db.list_eject_events("edge-a")) == 2
    assert db.list_eject_events("edge-b") == []


def test_eject_enforces_authentication_and_device_isolation(edge_client):
    client, token = edge_client
    payload = {"event_id": "evt-1", "event": "sheet_out", "source": "manual", "ts": 1}
    assert client.post("/api/edge/edge-a/eject", json=payload).status_code in (401, 403)
    assert client.post("/api/edge/edge-b/eject", json=payload,
                       headers=_headers(token)).status_code == 403
    assert client.get("/api/edge/edge-b/eject", headers=_headers(token)).status_code == 403

    merchant = _headers(SECRET_KEY)
    assert client.post("/api/edge/missing/eject", json=payload,
                       headers=merchant).status_code == 404
    assert client.get("/api/edge/missing/eject", headers=merchant).status_code == 404


def test_eject_rejects_invalid_payload_and_query(edge_client):
    client, token = edge_client
    headers = _headers(token)
    base = {"event_id": "evt-1", "event": "sheet_out", "source": "manual", "ts": 1}
    for updates in (
        {"event_id": ""},
        {"event": "other"},
        {"source": "automatic"},
        {"ts": 0},
        {"confidence": 1.1},
    ):
        response = client.post("/api/edge/edge-a/eject", json={**base, **updates},
                               headers=headers)
        assert response.status_code == 422
    assert client.get("/api/edge/edge-a/eject?limit=0", headers=headers).status_code == 422
    merchant = _headers(SECRET_KEY)
    assert client.post("/api/edge/missing/eject", json=base,
                       headers=merchant).status_code == 404


def test_activity_window_allows_printing_and_recent_completion_only(edge_client):
    client, token = edge_client
    headers = _headers(token)
    assert client.get("/api/edge/edge-a/activity", headers=headers).json()["data"]["active"] is False
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id="edge-a")
    db.set_job_status("f", "a.pdf", "printing")
    active = client.get("/api/edge/edge-a/activity", headers=headers).json()["data"]
    assert active["active"] is True
    assert active["status"] == "printing"
    db.complete_job(folder="f", filename="a.pdf")
    assert client.get("/api/edge/edge-a/activity", headers=headers).json()["data"]["status"] == "completed"


def test_eject_is_rejected_without_active_order(edge_client):
    client, token = edge_client
    response = client.post("/api/edge/edge-a/eject", json={
        "event_id": "inactive", "event": "sheet_out", "source": "vision", "ts": 1,
        "confidence": 0.9}, headers=_headers(token))
    assert response.status_code == 409


def test_event_id_conflict_does_not_overwrite_original(edge_client):
    client, token = edge_client
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id="edge-a")
    db.set_job_status("f", "a.pdf", "printing")
    headers = _headers(token)
    payload = {"event_id": "same-id", "event": "sheet_out", "source": "manual", "ts": 1}
    assert client.post("/api/edge/edge-a/eject", json=payload,
                       headers=headers).status_code == 200
    conflict = client.post("/api/edge/edge-a/eject",
                           json={**payload, "source": "vision"}, headers=headers)
    assert conflict.status_code == 409
    assert db.list_eject_events("edge-a")[0]["source"] == "manual"


JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body\xff\xd9"


def test_snapshot_is_stored_and_served(edge_client):
    client, token = edge_client
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id="edge-a")
    db.set_job_status("f", "a.pdf", "printing")
    headers = _headers(token)
    payload = {"event_id": "shot-1", "event": "sheet_out", "source": "vision", "ts": 5,
               "confidence": 0.8, "photo": base64.b64encode(JPEG).decode()}
    assert client.post("/api/edge/edge-a/eject", json=payload,
                       headers=headers).status_code == 200
    listed = client.get("/api/edge/edge-a/eject", headers=headers).json()["data"]["events"]
    assert listed[0]["has_photo"] is True
    photo = client.get("/api/edge/edge-a/eject/shot-1/photo", headers=headers)
    assert photo.status_code == 200
    assert photo.headers["content-type"].startswith("image/jpeg")
    assert photo.content == JPEG


def test_user_order_evidence_is_owned_and_order_scoped(edge_client):
    client, device_token = edge_client
    owner_id = db.create_user("evidence-owner", password_hash="password-hash")["id"]
    other_id = db.create_user("evidence-other", password_hash="password-hash")["id"]
    order_id = db.add_job({"folder": "owner-session", "filename": "owned.pdf",
                           "status": "pending", "user_id": owner_id}, printer_id="edge-a")
    db.set_job_status("owner-session", "owned.pdf", "printing")
    headers = {"Authorization": "Bearer " + create_user_token(owner_id)}
    payload = {"event_id": "order-photo", "event": "sheet_out", "source": "vision",
               "ts": 8, "photo": base64.b64encode(JPEG).decode()}
    assert client.post("/api/edge/edge-a/eject", json=payload,
                       headers=_headers(device_token)).status_code == 200
    assert db.list_eject_events_for_order(order_id)[0]["order_id"] == order_id
    events = client.get(f"/api/user/order/{order_id}/eject", headers=headers)
    assert events.status_code == 200
    assert events.json()["data"]["events"][0]["has_photo"] is True
    photo = client.get(events.json()["data"]["events"][0]["photo_url"], headers=headers)
    assert photo.status_code == 200
    assert photo.content == JPEG
    other_headers = {"Authorization": "Bearer " + create_user_token(other_id)}
    assert client.get(f"/api/user/order/{order_id}/eject",
                      headers=other_headers).status_code == 404
    assert client.get(f"/api/user/order/{order_id}/eject/order-photo/photo",
                      headers=other_headers).status_code == 404


def test_user_order_evidence_reports_no_photo(edge_client):
    client, device_token = edge_client
    owner_id = db.create_user("no-photo-owner", password_hash="password-hash")["id"]
    order_id = db.add_job({"folder": "no-photo-session", "filename": "no-photo.pdf",
                           "status": "pending", "user_id": owner_id}, printer_id="edge-a")
    db.set_job_status("no-photo-session", "no-photo.pdf", "printing")
    assert client.post("/api/edge/edge-a/eject", json={
        "event_id": "without-photo", "event": "sheet_out", "source": "vision", "ts": 9
    }, headers=_headers(device_token)).status_code == 200
    response = client.get(f"/api/user/order/{order_id}/eject", headers={
        "Authorization": "Bearer " + create_user_token(owner_id)
    })
    assert response.status_code == 200
    assert response.json()["data"]["events"][0]["has_photo"] is False
    assert response.json()["data"]["events"][0]["photo_url"] is None


def test_invalid_snapshot_is_ignored_but_event_kept(edge_client):
    client, token = edge_client
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id="edge-a")
    db.set_job_status("f", "a.pdf", "printing")
    headers = _headers(token)
    payload = {"event_id": "shot-2", "event": "sheet_out", "source": "vision", "ts": 6,
               "confidence": 0.5, "photo": base64.b64encode(b"not-a-jpeg").decode()}
    assert client.post("/api/edge/edge-a/eject", json=payload,
                       headers=headers).status_code == 200
    listed = client.get("/api/edge/edge-a/eject", headers=headers).json()["data"]["events"]
    assert listed[0]["has_photo"] is False
    assert client.get("/api/edge/edge-a/eject/shot-2/photo",
                      headers=headers).status_code == 404


def test_photo_requires_matching_device(edge_client):
    client, token = edge_client
    db.add_job({"folder": "f", "filename": "a.pdf", "status": "pending"}, printer_id="edge-a")
    db.set_job_status("f", "a.pdf", "printing")
    client.post("/api/edge/edge-a/eject", json={
        "event_id": "shot-3", "event": "sheet_out", "source": "vision", "ts": 7,
        "confidence": 0.7, "photo": base64.b64encode(JPEG).decode()},
        headers=_headers(token))
    assert client.get("/api/edge/edge-b/eject/shot-3/photo",
                      headers=_headers(token)).status_code == 403
