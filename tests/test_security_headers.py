"""公网安全响应头：常规头必带，HSTS 只在 HTTPS 下追加。"""

from types import SimpleNamespace

from main import _security_headers_for


def _request(scheme="http", forwarded=""):
    headers = {"x-forwarded-proto": forwarded} if forwarded else {}
    return SimpleNamespace(headers=headers,
                           url=SimpleNamespace(scheme=scheme))


def test_security_headers_present():
    headers = _security_headers_for(_request())
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=(self)" in headers["Permissions-Policy"]
    csp = headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "object-src 'none'" in csp
    assert "Strict-Transport-Security" not in headers


def test_hsts_only_on_https():
    assert "Strict-Transport-Security" in _security_headers_for(
        _request(scheme="https"))
    assert "Strict-Transport-Security" in _security_headers_for(
        _request(forwarded="https"))
    # 反代常见写法：https,http 取第一个
    assert "Strict-Transport-Security" in _security_headers_for(
        _request(forwarded="https, http"))
    assert "Strict-Transport-Security" not in _security_headers_for(
        _request(forwarded="http"))
