"""Tests for src.oauth_broker — host-side OAuth refresh daemon.

Post-split architecture: the broker no longer terminates TLS or binds a
TCP port.  It exposes a handler-via-host_service over a Unix socket.
Tests here cover the refresh flow, the generic upstream-proxy action
(for ``/login`` traffic the jail can't dial directly), CA generation,
and self-check.
"""

from __future__ import annotations

import base64
import json
import shutil
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from src import oauth_broker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def creds_file(tmp_path: Path) -> Path:
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": "old-refresh",
                    "expiresAt": int(time.time() * 1000) + 7_200_000,
                    "subscriptionType": "max",
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    return path


@pytest.fixture
def broker_dirs(tmp_path: Path, monkeypatch):
    """Point broker paths at tmp_path so we don't touch the real host."""
    broker_root = tmp_path / "broker"
    broker_root.mkdir()
    monkeypatch.setattr(oauth_broker, "BROKER_DIR", broker_root)
    monkeypatch.setattr(oauth_broker, "CA_CRT", broker_root / "ca.crt")
    monkeypatch.setattr(oauth_broker, "CA_KEY", broker_root / "ca.key")
    monkeypatch.setattr(oauth_broker, "SERVER_CRT", broker_root / "server.crt")
    monkeypatch.setattr(oauth_broker, "SERVER_KEY", broker_root / "server.key")
    monkeypatch.setattr(oauth_broker, "REFRESH_LOCK", broker_root / "refresh.lock")
    # Point the host-creds default at a nonexistent tmp path so
    # do_refresh's host-mirror path never touches the real ~/.claude.
    monkeypatch.setattr(
        oauth_broker, "DEFAULT_HOST_CREDS_PATH", broker_root / "nohost.json"
    )
    return broker_root


# ---------------------------------------------------------------------------
# _cached_tokens
# ---------------------------------------------------------------------------


def test_cached_tokens_returns_fresh(creds_file: Path):
    out = oauth_broker._cached_tokens(creds_file)
    assert out is not None
    assert out["accessToken"] == "old-access"


def test_cached_tokens_returns_none_when_near_expiry(tmp_path: Path):
    path = tmp_path / "creds.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "x",
                    "refreshToken": "y",
                    "expiresAt": int(time.time() * 1000) + 30_000,
                }
            }
        )
    )
    assert oauth_broker._cached_tokens(path) is None


def test_cached_tokens_returns_none_when_missing(tmp_path: Path):
    assert oauth_broker._cached_tokens(tmp_path / "nope.json") is None


def test_cached_tokens_returns_none_when_corrupt(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert oauth_broker._cached_tokens(path) is None


# ---------------------------------------------------------------------------
# do_refresh — the new primary API
# ---------------------------------------------------------------------------


def test_do_refresh_cache_hit_does_not_call_upstream(
    creds_file: Path, broker_dirs: Path
):
    with patch.object(oauth_broker, "_refresh_upstream") as m:
        resp = oauth_broker.do_refresh(creds_file)
    m.assert_not_called()
    assert resp["access_token"] == "old-access"
    assert resp["refresh_token"] == "old-refresh"
    assert resp["token_type"] == "Bearer"


def test_do_refresh_cache_miss_calls_upstream_and_writes(
    tmp_path: Path, broker_dirs: Path
):
    creds = tmp_path / "expired.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a-expired",
                    "refreshToken": "r-old",
                    "expiresAt": int(time.time() * 1000) - 10_000,
                    "subscriptionType": "max",
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    with patch.object(oauth_broker, "_refresh_upstream") as m:
        m.return_value = {
            "access_token": "a-new",
            "refresh_token": "r-new",
            "expires_in": 7200,
            "token_type": "Bearer",
        }
        resp = oauth_broker.do_refresh(creds)
    m.assert_called_once_with("r-old")
    assert resp["access_token"] == "a-new"

    # File was rewritten in-place (bind-mount inode preserved elsewhere).
    new = json.loads(creds.read_text())["claudeAiOauth"]
    assert new["accessToken"] == "a-new"
    assert new["refreshToken"] == "r-new"
    assert new["subscriptionType"] == "max"
    assert new["scopes"] == ["user:inference"]
    assert new["expiresAt"] > int(time.time() * 1000)


def test_do_refresh_mirrors_into_host_file_when_identity_matches(
    tmp_path: Path, broker_dirs: Path
):
    """When host Claude and the shared file share one refresh token, a
    successful refresh must also write the new tokens to the host file
    — otherwise host Claude Code keeps an invalidated token and the next
    /login dialog pops up unexpectedly."""
    shared = tmp_path / "shared.json"
    shared.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a-expired",
                    "refreshToken": "r-shared",
                    "expiresAt": int(time.time() * 1000) - 10_000,
                    "subscriptionType": "max",
                }
            }
        )
    )
    host = tmp_path / "host.json"
    host.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a-expired",
                    "refreshToken": "r-shared",  # same identity
                    "expiresAt": int(time.time() * 1000) - 10_000,
                    "subscriptionType": "max",
                    "hostOnlyField": "preserve-me",
                }
            }
        )
    )

    with patch.object(oauth_broker, "_refresh_upstream") as m:
        m.return_value = {
            "access_token": "a-new",
            "refresh_token": "r-new",
            "expires_in": 7200,
            "token_type": "Bearer",
        }
        oauth_broker.do_refresh(shared, host_creds_path=host)

    host_oauth = json.loads(host.read_text())["claudeAiOauth"]
    assert host_oauth["accessToken"] == "a-new"
    assert host_oauth["refreshToken"] == "r-new"
    assert host_oauth["hostOnlyField"] == "preserve-me"


def test_do_refresh_does_not_mirror_when_host_identity_differs(
    tmp_path: Path, broker_dirs: Path
):
    """Host Claude with an independent refresh token must be left alone —
    otherwise we'd log out a separate session the user had set up
    deliberately."""
    shared = tmp_path / "shared.json"
    shared.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a-expired",
                    "refreshToken": "r-shared",
                    "expiresAt": int(time.time() * 1000) - 10_000,
                }
            }
        )
    )
    host = tmp_path / "host.json"
    host_blob_before = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "host-access",
                "refreshToken": "r-different",
                "expiresAt": int(time.time() * 1000) + 3_600_000,
            }
        }
    )
    host.write_text(host_blob_before)

    with patch.object(oauth_broker, "_refresh_upstream") as m:
        m.return_value = {
            "access_token": "a-new",
            "refresh_token": "r-new",
            "expires_in": 7200,
        }
        oauth_broker.do_refresh(shared, host_creds_path=host)

    # Host file untouched.
    assert host.read_text() == host_blob_before


def test_do_refresh_returns_error_dict_when_no_refresh_token(
    tmp_path: Path, broker_dirs: Path
):
    creds = tmp_path / "empty.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "a",
                    "expiresAt": int(time.time() * 1000) - 1,
                }
            }
        )
    )
    resp = oauth_broker.do_refresh(creds)
    assert resp.get("error") == "no_refresh_token"


# ---------------------------------------------------------------------------
# do_proxy — generic upstream proxy (used for /login and future non-refresh
# paths the jail can't dial directly because of the --add-host loop)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_do_proxy_forwards_request_and_returns_b64_body(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["data"] = req.data
        return _FakeResp(
            200,
            {"Content-Type": "application/json", "X-Trace": "abc"},
            b'{"access_token":"tok"}',
        )

    monkeypatch.setattr(oauth_broker.urllib.request, "urlopen", fake_urlopen)
    out = oauth_broker.do_proxy(
        "POST",
        "/v1/oauth/token",
        {"Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20"},
        b'{"grant_type":"authorization_code","code":"xyz"}',
    )
    assert captured["url"] == "https://platform.claude.com/v1/oauth/token"
    assert captured["method"] == "POST"
    assert captured["data"] == b'{"grant_type":"authorization_code","code":"xyz"}'
    # urllib title-cases header names on ``header_items()`` — match case-insensitively.
    hdrs_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs_lower["content-type"] == "application/json"
    assert hdrs_lower["anthropic-beta"] == "oauth-2025-04-20"
    assert out["status"] == 200
    # Hop-by-hop headers must not leak in the response.
    assert "Content-Type" in out["headers"]
    assert base64.b64decode(out["body_b64"]) == b'{"access_token":"tok"}'


def test_do_proxy_strips_hop_by_hop_headers_on_request(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp(200, {}, b"")

    monkeypatch.setattr(oauth_broker.urllib.request, "urlopen", fake_urlopen)
    oauth_broker.do_proxy(
        "POST",
        "/v1/oauth/token",
        {
            "Host": "platform.claude.com",  # stripped; urllib sets Host from URL
            "Connection": "keep-alive",  # hop-by-hop
            "Content-Length": "42",  # recomputed
            "X-Keep": "me",
        },
        b"",
    )
    assert "host" not in captured["headers"]
    assert "connection" not in captured["headers"]
    assert "content-length" not in captured["headers"]
    assert captured["headers"].get("x-keep") == "me"


def test_do_proxy_passes_through_http_error_as_status(monkeypatch):
    """Upstream 4xx must surface verbatim — not become a 502 — so Claude
    Code sees the real error (e.g. ``invalid_grant``) instead of a
    broker-manufactured one."""
    import io

    def fake_urlopen(_req, timeout):
        raise urllib.error.HTTPError(
            url="https://platform.claude.com/v1/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={"Content-Type": "application/json"},
            fp=io.BytesIO(b'{"error":"invalid_grant"}'),
        )

    monkeypatch.setattr(oauth_broker.urllib.request, "urlopen", fake_urlopen)
    out = oauth_broker.do_proxy(
        "POST",
        "/v1/oauth/token",
        {"Content-Type": "application/json"},
        b'{"grant_type":"authorization_code","code":"bad"}',
    )
    assert out["status"] == 400
    assert base64.b64decode(out["body_b64"]) == b'{"error":"invalid_grant"}'


def test_do_proxy_returns_error_dict_on_network_failure(monkeypatch):
    def fake_urlopen(_req, timeout):
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr(oauth_broker.urllib.request, "urlopen", fake_urlopen)
    out = oauth_broker.do_proxy("GET", "/whatever", {}, b"")
    assert out.get("error") == "upstream_unreachable"
    assert "dns failure" in out.get("message", "")


def test_do_proxy_rejects_path_without_leading_slash():
    """Defensive — a relative path would make ``https://host + path``
    collapse into a different URL."""
    out = oauth_broker.do_proxy("GET", "v1/oauth/token", {}, b"")
    assert out.get("error") == "bad_path"


# ---------------------------------------------------------------------------
# Upstream User-Agent — avoid Cloudflare bot-signature bans (error 1010)
# ---------------------------------------------------------------------------


def _captured_headers(monkeypatch) -> dict:
    """Capture title-cased header items urllib would send upstream."""
    captured: dict = {}

    def fake_urlopen(req, timeout):
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp(
            200,
            {"Content-Type": "application/json"},
            b'{"access_token":"t","refresh_token":"r","expires_in":3600}',
        )

    monkeypatch.setattr(oauth_broker.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_refresh_upstream_sends_identifying_user_agent(monkeypatch):
    """The default urllib User-Agent is ``Python-urllib/3.X``, which
    Cloudflare's bot-signature layer blocks with error 1010.  That's
    exactly what broke ``claude /voice`` refresh on 2026-04-22.  Send
    a User-Agent that identifies us as the yolo-jail broker."""
    captured = _captured_headers(monkeypatch)
    oauth_broker._refresh_upstream("some-refresh-token")
    ua = captured["headers"].get("user-agent", "")
    assert ua, (
        "broker must set a User-Agent (default Python-urllib/* trips Cloudflare 1010)"
    )
    assert not ua.lower().startswith("python-"), (
        f"broker User-Agent {ua!r} still looks like the default urllib UA"
    )


def test_do_proxy_sends_identifying_user_agent_when_none_supplied(monkeypatch):
    """Same vector via the /login passthrough path.  If the client
    didn't send a User-Agent (or stripped ours), we must still send
    something non-default — otherwise Cloudflare blocks the proxied
    /v1/oauth/token call the same way."""
    captured = _captured_headers(monkeypatch)
    oauth_broker.do_proxy(
        "POST",
        "/v1/oauth/token",
        {"Content-Type": "application/json"},  # no UA
        b'{"grant_type":"authorization_code","code":"x"}',
    )
    ua = captured["headers"].get("user-agent", "")
    assert ua
    assert not ua.lower().startswith("python-")


def test_do_proxy_preserves_caller_supplied_user_agent(monkeypatch):
    """If the caller (Claude Code) sent a User-Agent, pass it through
    verbatim — don't clobber a real browser-style UA with our fallback."""
    captured = _captured_headers(monkeypatch)
    oauth_broker.do_proxy(
        "POST",
        "/v1/oauth/token",
        {"Content-Type": "application/json", "User-Agent": "claude-cli/2.1.101"},
        b"{}",
    )
    assert captured["headers"]["user-agent"] == "claude-cli/2.1.101"


def test_decode_proxy_request_validates_shape():
    ok = oauth_broker._decode_proxy_request(
        {
            "action": "proxy",
            "method": "POST",
            "path": "/v1/oauth/token",
            "headers": {"Content-Type": "application/json"},
            "body_b64": base64.b64encode(b"hi").decode(),
        }
    )
    assert isinstance(ok, dict)
    assert ok["method"] == "POST"
    assert ok["body"] == b"hi"

    assert isinstance(oauth_broker._decode_proxy_request({"path": "/x"}), str)
    assert isinstance(oauth_broker._decode_proxy_request({"method": "GET"}), str)
    assert isinstance(
        oauth_broker._decode_proxy_request(
            {"method": "POST", "path": "/x", "headers": "not-a-dict"}
        ),
        str,
    )
    assert isinstance(
        oauth_broker._decode_proxy_request(
            {"method": "POST", "path": "/x", "body_b64": "!!not-base64!!"}
        ),
        str,
    )


# ---------------------------------------------------------------------------
# Other pure helpers
# ---------------------------------------------------------------------------


def test_normalize_oauth_preserves_subscription(tmp_path: Path):
    prev = {
        "accessToken": "old",
        "refreshToken": "old-r",
        "expiresAt": 0,
        "subscriptionType": "max",
        "scopes": ["a", "b"],
    }
    upstream = {"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}
    out = oauth_broker._normalize_oauth(upstream, previous=prev)
    assert out["accessToken"] == "new"
    assert out["refreshToken"] == "new-r"
    assert out["subscriptionType"] == "max"
    assert out["scopes"] == ["a", "b"]


def test_normalize_oauth_keeps_previous_refresh_if_upstream_omits(tmp_path: Path):
    prev = {"accessToken": "old", "refreshToken": "keep-me", "expiresAt": 0}
    upstream = {"access_token": "new", "expires_in": 3600}
    out = oauth_broker._normalize_oauth(upstream, previous=prev)
    assert out["refreshToken"] == "keep-me"
    assert out["accessToken"] == "new"


def test_write_tokens_preserves_inode(tmp_path: Path):
    """Jails bind-mount this file; rewriting in-place must keep the same inode."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": 0}}))
    inode_before = path.stat().st_ino
    oauth_broker._write_tokens(path, {"accessToken": "b", "expiresAt": 1})
    assert path.stat().st_ino == inode_before
    assert json.loads(path.read_text())["claudeAiOauth"]["accessToken"] == "b"


# ---------------------------------------------------------------------------
# CA generation
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_ensure_ca_generates_ca_and_leaf(broker_dirs: Path):
    oauth_broker.ensure_ca_and_leaf()
    assert oauth_broker.CA_CRT.is_file()
    assert oauth_broker.CA_KEY.is_file()
    assert oauth_broker.SERVER_CRT.is_file()
    assert oauth_broker.SERVER_KEY.is_file()
    assert oauth_broker.CA_KEY.stat().st_mode & 0o777 == 0o600
    assert oauth_broker.SERVER_KEY.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_ensure_ca_idempotent(broker_dirs: Path):
    oauth_broker.ensure_ca_and_leaf()
    mtime = oauth_broker.CA_CRT.stat().st_mtime
    oauth_broker.ensure_ca_and_leaf()
    assert oauth_broker.CA_CRT.stat().st_mtime == mtime


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_ensure_ca_force_rotates(broker_dirs: Path):
    oauth_broker.ensure_ca_and_leaf()
    old_crt = oauth_broker.CA_CRT.read_bytes()
    oauth_broker.ensure_ca_and_leaf(force=True)
    assert oauth_broker.CA_CRT.read_bytes() != old_crt


def test_ensure_ca_clear_error_when_openssl_missing(broker_dirs: Path, monkeypatch):
    """Without openssl AND without state, ensure_ca_and_leaf must SystemExit
    with a single actionable line, not a deep subprocess traceback.

    Regression: the daemon previously crashed inside _openssl with
    FileNotFoundError, which yolo-claude-oauth-broker-host swallowed into
    a multi-frame traceback in the host-service log.
    """
    monkeypatch.setattr(oauth_broker, "_resolve_openssl", lambda: None)
    with pytest.raises(SystemExit) as excinfo:
        oauth_broker.ensure_ca_and_leaf()
    msg = str(excinfo.value)
    assert "openssl" in msg
    assert "PATH" in msg


def test_ensure_ca_skips_openssl_check_when_state_present(
    broker_dirs: Path, monkeypatch
):
    """If CA + leaf already exist, openssl absence at runtime is benign —
    don't refuse to run."""
    for p in (
        oauth_broker.CA_CRT,
        oauth_broker.CA_KEY,
        oauth_broker.SERVER_CRT,
        oauth_broker.SERVER_KEY,
    ):
        p.write_bytes(b"placeholder")
    monkeypatch.setattr(oauth_broker, "_resolve_openssl", lambda: None)
    oauth_broker.ensure_ca_and_leaf()  # must not raise


def test_resolve_openssl_falls_back_to_known_paths(monkeypatch, tmp_path):
    """When PATH is empty / stripped, _resolve_openssl must still find
    openssl via the absolute-path fallback list.

    Regression: the broker daemon was crash-looping with FileNotFoundError
    even though /usr/bin/openssl existed on the host, because the spawned
    daemon's PATH didn't include /usr/bin (some launcher layer was
    stripping it).
    """
    fake_openssl = tmp_path / "openssl"
    fake_openssl.write_text("#!/bin/sh\n")
    fake_openssl.chmod(0o755)
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _x: None)
    monkeypatch.setattr(oauth_broker, "_OPENSSL_FALLBACK_PATHS", (str(fake_openssl),))
    assert oauth_broker._resolve_openssl() == str(fake_openssl)


# ---------------------------------------------------------------------------
# self_check
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_self_check_ok(broker_dirs: Path, creds_file: Path, monkeypatch, capsys):
    oauth_broker.ensure_ca_and_leaf()
    monkeypatch.setattr(oauth_broker, "DEFAULT_CREDS_PATH", creds_file)
    rc = oauth_broker.self_check()
    assert rc == 0


def test_self_check_reports_missing_ca(broker_dirs: Path, capsys, monkeypatch):
    # Without openssl anywhere AND without CA files on disk, the user
    # has no recovery path (`--init-ca` won't work), so we fail hard.
    # See test_doctor_inactive_loopholes for the state-missing-but-
    # openssl-present happy path (returns rc=0 with warnings).
    #
    # Mock ``_resolve_openssl`` directly — broker's openssl resolution
    # falls back to a list of absolute paths (``/usr/bin/openssl`` etc.)
    # when ``shutil.which`` misses, so patching ``which`` alone isn't
    # enough on a CI runner that has openssl preinstalled.
    monkeypatch.setattr(oauth_broker, "_resolve_openssl", lambda: None)
    rc = oauth_broker.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "openssl" in out or "not yet generated" in out
