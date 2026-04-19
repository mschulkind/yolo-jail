"""Tests for src.oauth_broker — host-side OAuth refresh daemon.

Post-split architecture: the broker no longer terminates TLS or binds a
TCP port.  It exposes a handler-via-host_service over a Unix socket.
Tests here cover the refresh flow, CA generation, and self-check.
"""

from __future__ import annotations

import json
import shutil
import time
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
    # Without openssl on PATH AND without CA files on disk, the user
    # has no recovery path (`--init-ca` won't work), so we fail hard.
    # See test_doctor_inactive_loopholes for the state-missing-but-
    # openssl-present happy path (returns rc=0 with warnings).
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _x: None)
    rc = oauth_broker.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "openssl" in out or "not yet generated" in out
