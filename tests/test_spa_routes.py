"""SPA 深链接：直接打开前端路由地址时必须返回 index.html。"""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_login_page_served():
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_register_page_served():
    resp = client.get("/register")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_intro_redirects_to_trailing_slash():
    resp = client.get("/intro", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/intro/"


def test_intro_landing_page_served():
    resp = client.get("/intro/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "思印客" in resp.text
