"""Tests for src.host_processes — the host-processes loophole daemon."""

from __future__ import annotations

import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src import host_processes, host_service


# macOS ps is BSD-style and doesn't understand `-C <comm>`; the daemon's
# own logic works, but the end-to-end "does ps run" tests are Linux-only.
_PS_C_SUPPORTED = sys.platform.startswith("linux")
_SKIP_MACOS_PS = pytest.mark.skipif(
    not _PS_C_SUPPORTED, reason="macOS ps doesn't support -C <comm>"
)


def _send_request(conn: socket.socket, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    conn.sendall(struct.pack(">I", len(body)))
    conn.sendall(body)


def _read_all_frames(conn: socket.socket, timeout: float = 2.0):
    conn.settimeout(timeout)
    buf = bytearray()
    while True:
        try:
            chunk = conn.recv(4096)
        except socket.timeout:
            return
        if not chunk:
            return
        buf.extend(chunk)
        while len(buf) >= 5:
            stream_id, length = struct.unpack(">BI", bytes(buf[:5]))
            if len(buf) < 5 + length:
                break
            payload = bytes(buf[5 : 5 + length])
            del buf[: 5 + length]
            yield stream_id, payload


def _collect(conn: socket.socket):
    stdout = b""
    stderr = b""
    rc = None
    for s, p in _read_all_frames(conn):
        if s == host_service.STREAM_STDOUT:
            stdout += p
        elif s == host_service.STREAM_STDERR:
            stderr += p
        elif s == host_service.STREAM_EXIT:
            (rc,) = struct.unpack(">i", p)
    return stdout.decode(errors="replace"), stderr.decode(errors="replace"), rc


@pytest.fixture
def started_daemon(tmp_path: Path):
    # Socket path under /tmp to stay under macOS AF_UNIX 104-byte cap;
    # the config file can use pytest's tmp_path (any length).
    import shutil as _shutil
    import tempfile

    sockdir = Path(tempfile.mkdtemp(prefix="yjtp-", dir="/tmp"))
    sock = sockdir / "s.sock"
    cfg = tmp_path / "yolo-jail.jsonc"
    cfg.write_text(
        json.dumps(
            {
                "host_processes": {
                    "visible": ["sleep", "cat"],  # shell builtins won't hit
                    "fields": ["pid", "comm", "args"],
                }
            }
        )
    )

    handler = host_processes.build_handler(cfg)
    t = threading.Thread(target=host_service.serve, args=(handler, sock), daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.1)
                probe.connect(str(sock))
                probe.close()
                break
            except OSError:
                pass
        time.sleep(0.02)
    assert sock.exists()
    try:
        yield sock, cfg
    finally:
        _shutil.rmtree(sockdir, ignore_errors=True)


def _client(sock: Path) -> socket.socket:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(sock))
    return c


@_SKIP_MACOS_PS
def test_list_mode_runs_ps_for_allowlisted_comm(started_daemon):
    sock, _ = started_daemon
    # Start a sleep process we can observe.
    import subprocess

    p = subprocess.Popen(["sleep", "5"])
    time.sleep(0.1)
    try:
        c = _client(sock)
        _send_request(c, {"jail_id": "test", "mode": "list"})
        stdout, stderr, rc = _collect(c)
        c.close()
    finally:
        p.kill()
        p.wait()

    assert rc == 0, (stdout, stderr)
    # The ps output should contain our sleep PID.
    assert str(p.pid) in stdout or "sleep" in stdout


@_SKIP_MACOS_PS
def test_pid_mode_rejects_non_allowlisted_comm(started_daemon):
    sock, _ = started_daemon
    # Our own PID is python — not allowlisted in the fixture.
    import os

    c = _client(sock)
    _send_request(c, {"jail_id": "test", "mode": "pid", "pid": os.getpid()})
    stdout, stderr, rc = _collect(c)
    c.close()

    assert rc == 2
    assert "not allowlisted" in stderr


@_SKIP_MACOS_PS
def test_pid_mode_rejects_nonexistent_pid(started_daemon):
    sock, _ = started_daemon
    c = _client(sock)
    _send_request(c, {"jail_id": "test", "mode": "pid", "pid": 999_999})
    _stdout, stderr, rc = _collect(c)
    c.close()

    assert rc == 1
    assert "not found" in stderr


def test_empty_visible_list_fails_gracefully(tmp_path: Path):
    import tempfile

    sockdir = Path(tempfile.mkdtemp(prefix="yjtp-", dir="/tmp"))
    sock = sockdir / "s.sock"
    cfg = tmp_path / "yolo-jail.jsonc"
    cfg.write_text(json.dumps({"host_processes": {"visible": []}}))

    handler = host_processes.build_handler(cfg)
    t = threading.Thread(target=host_service.serve, args=(handler, sock), daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if sock.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.1)
                probe.connect(str(sock))
                probe.close()
                break
            except OSError:
                pass
        time.sleep(0.02)

    c = _client(sock)
    _send_request(c, {"jail_id": "test", "mode": "list"})
    _stdout, stderr, rc = _collect(c)
    c.close()

    assert rc == 3
    assert "empty" in stderr or "nothing to show" in stderr


def test_unknown_mode_rejected(started_daemon):
    sock, _ = started_daemon
    c = _client(sock)
    _send_request(c, {"jail_id": "test", "mode": "hacker-mode"})
    _stdout, stderr, rc = _collect(c)
    c.close()

    assert rc == 2
    assert "unknown mode" in stderr


def test_self_check_missing_env_falls_back_to_cwd(monkeypatch, capsys, tmp_path):
    """No env var + no yolo-jail.jsonc in CWD → OK (loophole is installed,
    just nothing to report)."""
    monkeypatch.delenv("YOLO_HOST_PROCESSES_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # empty tmp dir = no yolo-jail.jsonc
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "no host_processes config" in out


def test_self_check_missing_file(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("YOLO_HOST_PROCESSES_CONFIG", str(tmp_path / "nope.jsonc"))
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "not found" in out


def test_self_check_empty_visible_is_ok(tmp_path: Path, monkeypatch, capsys):
    """Empty allowlist isn't a failure — daemon just serves nothing.
    Misconfiguration to look at, not a reason to fail doctor."""
    cfg = tmp_path / "c.jsonc"
    cfg.write_text(json.dumps({"host_processes": {"visible": []}}))
    monkeypatch.setenv("YOLO_HOST_PROCESSES_CONFIG", str(cfg))
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "no host_processes.visible" in out


def test_self_check_ok(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text(
        json.dumps({"host_processes": {"visible": ["layout-manager", "sway"]}})
    )
    monkeypatch.setenv("YOLO_HOST_PROCESSES_CONFIG", str(cfg))
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 comms" in out
