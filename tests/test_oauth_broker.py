"""Tests for src.oauth_broker — refresh logic + CA generation."""

from __future__ import annotations

import json
import shutil
import socket
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src import oauth_broker


# ---------------------------------------------------------------------------
# Refresh logic (cached hit, cache miss, flock) without real HTTP
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
                    "expiresAt": int(time.time() * 1000) + 7_200_000,  # +2h
                    "subscriptionType": "max",
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    return path


@pytest.fixture
def broker_dirs(tmp_path: Path, monkeypatch):
    """Point broker paths at tmp_path so we don't touch the real ~/.local/share."""
    broker_root = tmp_path / "broker"
    broker_root.mkdir()
    monkeypatch.setattr(oauth_broker, "BROKER_DIR", broker_root)
    monkeypatch.setattr(oauth_broker, "CA_CRT", broker_root / "ca.crt")
    monkeypatch.setattr(oauth_broker, "CA_KEY", broker_root / "ca.key")
    monkeypatch.setattr(oauth_broker, "SERVER_CRT", broker_root / "server.crt")
    monkeypatch.setattr(oauth_broker, "SERVER_KEY", broker_root / "server.key")
    monkeypatch.setattr(oauth_broker, "REFRESH_LOCK", broker_root / "refresh.lock")
    return broker_root


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
                    "expiresAt": int(time.time() * 1000) + 30_000,  # 30s
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


def test_handle_refresh_cache_hit_does_not_call_upstream(
    creds_file: Path, broker_dirs: Path
):
    with patch.object(oauth_broker, "_refresh_upstream") as m:
        status, body = oauth_broker.handle_refresh(creds_file)
    m.assert_not_called()
    assert status == 200
    resp = json.loads(body)
    assert resp["access_token"] == "old-access"
    assert resp["refresh_token"] == "old-refresh"
    assert resp["token_type"] == "Bearer"


def test_handle_refresh_cache_miss_calls_upstream_and_writes(
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
        status, body = oauth_broker.handle_refresh(creds)
    m.assert_called_once_with("r-old")
    assert status == 200
    resp = json.loads(body)
    assert resp["access_token"] == "a-new"

    # File was written in-place and preserves subscriptionType/scopes.
    new = json.loads(creds.read_text())["claudeAiOauth"]
    assert new["accessToken"] == "a-new"
    assert new["refreshToken"] == "r-new"
    assert new["subscriptionType"] == "max"
    assert new["scopes"] == ["user:inference"]
    assert new["expiresAt"] > int(time.time() * 1000)


def test_handle_refresh_no_refresh_token(tmp_path: Path, broker_dirs: Path):
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
    status, body = oauth_broker.handle_refresh(creds)
    assert status == 400
    assert b"no_refresh_token" in body


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
    prev = {
        "accessToken": "old",
        "refreshToken": "keep-me",
        "expiresAt": 0,
    }
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


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_self_check_ok(broker_dirs: Path, creds_file: Path, monkeypatch, capsys):
    # Mask the systemd-state probe and TCP-probe — they check operational
    # state of a running broker, not the file-level prereqs this test
    # exercises.  No daemon is running on a CI machine, but the file
    # checks should still come out green.
    oauth_broker.ensure_ca_and_leaf()
    from src import claude_refresher

    monkeypatch.setattr(claude_refresher, "DEFAULT_CREDS_PATH", creds_file)
    monkeypatch.setattr(oauth_broker, "_systemd_check", lambda: [])
    monkeypatch.setattr(oauth_broker, "_tcp_probe", lambda h, p, timeout=2.0: [])
    rc = oauth_broker.self_check()
    assert rc == 0


def test_self_check_reports_missing_ca(broker_dirs: Path, capsys, monkeypatch):
    # Make systemd check a no-op and TCP probe fail silently to isolate CA miss.
    monkeypatch.setattr(oauth_broker, "_systemd_check", lambda: [])
    monkeypatch.setattr(
        oauth_broker, "_tcp_probe", lambda h, p, timeout=2.0: ["probe skip"]
    )
    rc = oauth_broker.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "missing" in out


# ---------------------------------------------------------------------------
# Systemd + TCP probe diagnostics
# ---------------------------------------------------------------------------


def test_tcp_probe_flags_unreachable():
    # Port 1 on an unroutable address is guaranteed to fail fast in CI.
    out = oauth_broker._tcp_probe("127.0.0.1", 1, timeout=0.5)
    assert len(out) == 1
    assert "cannot reach broker" in out[0]


def test_tcp_probe_ok_when_listening(tmp_path: Path):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert oauth_broker._tcp_probe("127.0.0.1", port, timeout=1.0) == []
    finally:
        s.close()


def test_systemd_check_silent_when_systemctl_missing(monkeypatch):
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _: None)
    assert oauth_broker._systemd_check() == []


def test_systemd_check_silent_when_unit_missing(monkeypatch):
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _: "/usr/bin/systemctl")
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        # First call: `systemctl cat …` → rc=1 (no unit file)
        return _MockProc(returncode=1, stdout="", stderr="No such unit")

    monkeypatch.setattr(oauth_broker.subprocess, "run", fake_run)
    assert oauth_broker._systemd_check() == []
    assert calls["n"] == 1


def test_systemd_check_diagnoses_port_443(monkeypatch):
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _: "/usr/bin/systemctl")
    responses = iter(
        [
            _MockProc(returncode=0),  # cat → unit exists
            _MockProc(returncode=3, stdout="failed"),  # is-active
            _MockProc(
                returncode=0,
                stdout=(
                    "Apr 17 18:00:00 host bin[1]: Permission denied binding to :443\n"
                ),
            ),  # journalctl
        ]
    )
    monkeypatch.setattr(
        oauth_broker.subprocess, "run", lambda *a, **kw: next(responses)
    )
    out = oauth_broker._systemd_check()
    assert len(out) == 1
    assert "port 443 bind denied" in out[0]
    assert "ip_unprivileged_port_start" in out[0]
    assert "--port 8443" in out[0]


def test_systemd_check_diagnoses_port_in_use(monkeypatch):
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _: "/usr/bin/systemctl")
    responses = iter(
        [
            _MockProc(returncode=0),
            _MockProc(returncode=3, stdout="failed"),
            _MockProc(
                returncode=0,
                stdout="OSError: [Errno 98] Address already in use\n",
            ),
        ]
    )
    monkeypatch.setattr(
        oauth_broker.subprocess, "run", lambda *a, **kw: next(responses)
    )
    out = oauth_broker._systemd_check()
    assert "already bound" in out[0]


def test_systemd_check_active_returns_empty(monkeypatch):
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _: "/usr/bin/systemctl")
    responses = iter(
        [
            _MockProc(returncode=0),  # cat
            _MockProc(returncode=0, stdout="active"),  # is-active
        ]
    )
    monkeypatch.setattr(
        oauth_broker.subprocess, "run", lambda *a, **kw: next(responses)
    )
    assert oauth_broker._systemd_check() == []


class _MockProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
