"""提交 9：两个打印机客户端并发抢单，不能出现同一单被两台机器同时拿走。"""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import db
from fastapi.testclient import TestClient
from main import app
from print_queue import print_queue_singleton


def _device(device_id):
    db.register_device(device_id, name=device_id)
    token = device_id + "-token"
    conn = db._connect()
    try:
        conn.execute(
            "INSERT INTO device_tokens (device_id, token_hash, created_at, revoked) "
            "VALUES (?, ?, 1, 0)",
            (device_id, hashlib.sha256(token.encode()).hexdigest()),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def _seed(count, prefix="c"):
    queue = print_queue_singleton().data
    queue.clear()
    for i in range(count):
        job = {"folder": "S%d" % i, "filename": "%s%d.pdf" % (prefix, i),
               "print_copies": 1, "print_range_start": 1, "print_range_end": 1,
               "print_side": "one-sided", "status": "pending"}
        job["id"] = db.add_job(dict(job))
        queue.append(job)
    return queue


def _take(client, token, device):
    response = client.get("/printer/jobs", params={"device_id": device},
                          headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    return (response.json() or {}).get("data", {}).get("jobs")


def _report_printing(client, token, device, job):
    """模拟客户端下载完成后上报 printing：后端会把该单出队，之后不再派发。"""
    response = client.post(
        "/printer/job/state",
        json={"filename": job["filename"], "folder": job["folder"],
              "state": "printing", "authentication": token},
        headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200


def test_two_clients_never_share_a_job(temp_db):
    _device("cc-a")
    _device("cc-b")
    _seed(30)
    try:
        clients = {"cc-a": (TestClient(app), "cc-a-token"),
                   "cc-b": (TestClient(app), "cc-b-token")}

        def worker(device):
            client, token = clients[device]
            got = []
            for _ in range(60):
                job = _take(client, token, device)
                if not job:
                    break
                got.append((job["folder"], job["filename"], device))
                _report_printing(client, token, device, job)
            return got

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ["cc-a", "cc-b"]))

        assigned = [item for chunk in results for item in chunk]
        keys = [(folder, filename) for folder, filename, _ in assigned]
        # 每一单只能被返回一次：并发下不允许重复取到
        assert len(keys) == len(set(keys))
        assert len(keys) == 30
        # 数据库归属与返回值一致，且每单只属于一台设备
        owners = {}
        for folder, filename, device in assigned:
            job = db.get_job(folder, filename)
            assert job["printer_id"] == device
            assert job["status"] == "printing"
            owners.setdefault(device, set()).add(filename)
        assert owners.get("cc-a") and owners.get("cc-b")
        assert not (owners["cc-a"] & owners["cc-b"])
    finally:
        print_queue_singleton().data.clear()


def test_device_scoped_job_cannot_be_taken_by_other(temp_db):
    _device("cc-a")
    _device("cc-b")
    queue = print_queue_singleton().data
    queue.clear()
    job = {"folder": "SX", "filename": "mine.pdf", "print_copies": 1,
           "print_range_start": 1, "print_range_end": 1,
           "print_side": "one-sided", "status": "pending"}
    job["id"] = db.add_job(dict(job), printer_id="cc-a")
    job["printer_id"] = "cc-a"
    queue.append(job)
    try:
        client = TestClient(app)
        assert _take(client, "cc-b-token", "cc-b") is None
        taken = _take(client, "cc-a-token", "cc-a")
        assert taken and taken["filename"] == "mine.pdf"
    finally:
        queue.clear()
