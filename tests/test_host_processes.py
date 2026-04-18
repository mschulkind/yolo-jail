"""Tests for src.host_processes — the host-processes loophole daemon."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src import host_processes, host_service


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
    sock = tmp_path / "host-processes.sock"
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
    yield sock, cfg


def _client(sock: Path) -> socket.socket:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(sock))
    return c


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


def test_pid_mode_rejects_nonexistent_pid(started_daemon):
    sock, _ = started_daemon
    c = _client(sock)
    _send_request(c, {"jail_id": "test", "mode": "pid", "pid": 999_999})
    _stdout, stderr, rc = _collect(c)
    c.close()

    assert rc == 1
    assert "not found" in stderr


def test_empty_visible_list_fails_gracefully(tmp_path: Path):
    sock = tmp_path / "s.sock"
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


def test_self_check_missing_env(monkeypatch, capsys):
    monkeypatch.delenv("YOLO_HOST_PROCESSES_CONFIG", raising=False)
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "YOLO_HOST_PROCESSES_CONFIG" in out


def test_self_check_missing_file(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("YOLO_HOST_PROCESSES_CONFIG", str(tmp_path / "nope.jsonc"))
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "not found" in out


def test_self_check_empty_visible(tmp_path: Path, monkeypatch, capsys):
    cfg = tmp_path / "c.jsonc"
    cfg.write_text(json.dumps({"host_processes": {"visible": []}}))
    monkeypatch.setenv("YOLO_HOST_PROCESSES_CONFIG", str(cfg))
    rc = host_processes.self_check()
    out = capsys.readouterr().out
    assert rc == 1
    assert "empty" in out


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
