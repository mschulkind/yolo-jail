"""Tests for src.oauth_broker_jail — the in-jail TLS terminator.

The big regressions we lock in here:

1.  Only ``grant_type=refresh_token`` requests route through the host
    broker's refresh flow.  Every other grant (most importantly
    ``authorization_code`` from ``/login``) and any non-
    ``/v1/oauth/token`` path must proxy upstream, or ``/login`` returns
    400 with ``no_refresh_token`` on a logged-out jail.

2.  The upstream proxy goes *through* the host broker via the unix
    socket — not via ``urllib`` direct from the jail.  ``--add-host``
    maps ``platform.claude.com`` back to this daemon, so a direct
    upstream dial loops back and the whole request returns 502
    ``upstream_unreachable``.  The host has real DNS.
"""

from __future__ import annotations

import base64
import json

from src import oauth_broker_jail


# ---------------------------------------------------------------------------
# _is_refresh_grant — the routing predicate
# ---------------------------------------------------------------------------


def test_is_refresh_grant_true_for_refresh_token():
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": "abc"}).encode()
    assert oauth_broker_jail._is_refresh_grant(body) is True


def test_is_refresh_grant_false_for_authorization_code():
    """/login posts ``grant_type=authorization_code`` — the routing bug
    treated this as a refresh and returned 400.  Must route to the
    proxy, not the broker."""
    body = json.dumps({"grant_type": "authorization_code", "code": "xyz"}).encode()
    assert oauth_broker_jail._is_refresh_grant(body) is False


def test_is_refresh_grant_false_for_empty_body():
    assert oauth_broker_jail._is_refresh_grant(b"") is False


def test_is_refresh_grant_false_for_non_json_body():
    """A malformed body (e.g. form-urlencoded) must not accidentally
    match — let upstream return its own error."""
    assert oauth_broker_jail._is_refresh_grant(b"grant_type=refresh_token") is False


def test_is_refresh_grant_false_for_json_non_object():
    assert oauth_broker_jail._is_refresh_grant(b'"refresh_token"') is False
    assert oauth_broker_jail._is_refresh_grant(b"[]") is False


def test_is_refresh_grant_false_when_grant_type_missing():
    body = json.dumps({"refresh_token": "abc"}).encode()
    assert oauth_broker_jail._is_refresh_grant(body) is False


# ---------------------------------------------------------------------------
# _proxy_upstream — routes through the host broker, not urllib
# ---------------------------------------------------------------------------


def test_proxy_upstream_sends_proxy_action_to_host_broker(monkeypatch):
    """The whole point of this change: the jail never dials upstream
    directly.  Confirm the request we build carries ``action=proxy`` and
    base64-encoded body, and that the host broker's response (status,
    headers, body) round-trips verbatim to the caller."""
    captured: dict = {}

    def fake_ask(socket_path, request):
        captured["socket_path"] = socket_path
        captured["request"] = request
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json", "X-Trace": "abc"},
            "body_b64": base64.b64encode(b'{"access_token":"tok"}').decode(),
        }

    monkeypatch.setattr(oauth_broker_jail, "ask_host_broker", fake_ask)
    status, headers, body = oauth_broker_jail._proxy_upstream(
        "/run/yolo-services/claude-oauth-broker.sock",
        "POST",
        "/v1/oauth/token",
        {"Content-Type": "application/json"},
        b'{"grant_type":"authorization_code","code":"x"}',
    )
    assert captured["socket_path"] == "/run/yolo-services/claude-oauth-broker.sock"
    assert captured["request"]["action"] == "proxy"
    assert captured["request"]["method"] == "POST"
    assert captured["request"]["path"] == "/v1/oauth/token"
    assert (
        base64.b64decode(captured["request"]["body_b64"])
        == b'{"grant_type":"authorization_code","code":"x"}'
    )
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert body == b'{"access_token":"tok"}'


def test_proxy_upstream_returns_502_when_host_broker_fails(monkeypatch):
    """If the host broker connection itself breaks (socket gone, protocol
    error), surface a 502 so Claude Code sees a real failure — and include
    the detail so the operator can debug."""

    def fake_ask(_socket_path, _request):
        raise RuntimeError("host broker closed without an exit frame")

    monkeypatch.setattr(oauth_broker_jail, "ask_host_broker", fake_ask)
    status, headers, body = oauth_broker_jail._proxy_upstream(
        "/tmp/nope.sock", "GET", "/whatever", {}, b""
    )
    assert status == 502
    assert headers["Content-Type"] == "application/json"
    parsed = json.loads(body)
    assert parsed["error"] == "broker_unavailable"
    assert "host broker closed" in parsed["detail"]


def test_proxy_upstream_returns_502_on_upstream_error_dict(monkeypatch):
    """Host broker surfacing ``{error: "upstream_unreachable"}`` means the
    real ``platform.claude.com`` was unreachable.  Pass that back as 502
    with the detail so the user sees the real network error."""

    def fake_ask(_socket_path, _request):
        return {"error": "upstream_unreachable", "message": "name or service not known"}

    monkeypatch.setattr(oauth_broker_jail, "ask_host_broker", fake_ask)
    status, _headers, body = oauth_broker_jail._proxy_upstream(
        "/tmp/nope.sock", "GET", "/whatever", {}, b""
    )
    assert status == 502
    parsed = json.loads(body)
    assert parsed["error"] == "upstream_unreachable"


def test_proxy_upstream_handles_empty_body(monkeypatch):
    """GETs have no body; we shouldn't send a stray base64 ``=`` chunk."""
    captured: dict = {}

    def fake_ask(_socket_path, request):
        captured["body_b64"] = request["body_b64"]
        return {"status": 204, "headers": {}, "body_b64": ""}

    monkeypatch.setattr(oauth_broker_jail, "ask_host_broker", fake_ask)
    status, _headers, body = oauth_broker_jail._proxy_upstream(
        "/tmp/s.sock", "GET", "/v1/me", {}, b""
    )
    assert captured["body_b64"] == ""
    assert status == 204
    assert body == b""
