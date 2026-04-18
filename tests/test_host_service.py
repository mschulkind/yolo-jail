"""Tests for src.host_service — the loophole daemon helper library."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from src import host_service


def _send_request(conn: socket.socket, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    conn.sendall(struct.pack(">I", len(body)))
    conn.sendall(body)


def _read_frames(conn: socket.socket, timeout: float = 2.0):
    """Read frames until EOF.  Yields (stream_id, payload)."""
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


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    return tmp_path / "svc.sock"


def _start_server(handler, sock_path: Path) -> threading.Thread:
    t = threading.Thread(
        target=host_service.serve, args=(handler, sock_path), daemon=True
    )
    t.start()
    # `bind()` creates the socket file before `listen()` completes, so
    # `sock_path.exists()` going true is necessary but not sufficient —
    # connections arriving before listen() get ECONNREFUSED. Wait for a
    # probe connect to succeed before returning.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if sock_path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.1)
                probe.connect(str(sock_path))
                probe.close()
                return t
            except OSError:
                pass
        time.sleep(0.02)
    raise AssertionError("server did not start accepting")


def _client(sock_path: Path) -> socket.socket:
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(sock_path))
    return c


def test_roundtrip_simple_stdout_and_exit(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        session.stdout("hello\n")
        session.exit(0)

    _start_server(handler, socket_path)
    c = _client(socket_path)
    try:
        _send_request(c, {"jail_id": "test-jail"})
        frames = list(_read_frames(c))
    finally:
        c.close()

    streams = [s for s, _ in frames]
    assert host_service.STREAM_STDOUT in streams
    assert host_service.STREAM_EXIT in streams
    stdout = b"".join(p for s, p in frames if s == host_service.STREAM_STDOUT)
    assert stdout == b"hello\n"
    (_, exit_payload) = next((s, p) for s, p in frames if s == host_service.STREAM_EXIT)
    (rc,) = struct.unpack(">i", exit_payload)
    assert rc == 0


def test_session_json_emits_line(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        session.json({"ok": True, "echo": session.request.get("x")})

    _start_server(handler, socket_path)
    c = _client(socket_path)
    try:
        _send_request(c, {"jail_id": "j", "x": 42})
        frames = list(_read_frames(c))
    finally:
        c.close()

    stdout = b"".join(p for s, p in frames if s == host_service.STREAM_STDOUT).decode()
    assert stdout.endswith("\n")
    parsed = json.loads(stdout)
    assert parsed == {"ok": True, "echo": 42}


def test_handler_exception_reports_stderr_and_exit1(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        raise RuntimeError("boom")

    _start_server(handler, socket_path)
    c = _client(socket_path)
    try:
        _send_request(c, {"jail_id": "j"})
        frames = list(_read_frames(c))
    finally:
        c.close()

    stderr = b"".join(p for s, p in frames if s == host_service.STREAM_STDERR).decode()
    assert "boom" in stderr
    (_, exit_payload) = next((s, p) for s, p in frames if s == host_service.STREAM_EXIT)
    (rc,) = struct.unpack(">i", exit_payload)
    assert rc == 1


def test_exec_allowlisted_enforces_allowlist(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        # Client's 'thing' selector is not in the allowlist; should reject.
        session.exec_allowlisted(
            lambda req: ["echo", req["thing"]],
            allowlist={"safe-value"},
        )

    _start_server(handler, socket_path)
    c = _client(socket_path)
    try:
        _send_request(c, {"jail_id": "j", "thing": "not-allowed"})
        frames = list(_read_frames(c))
    finally:
        c.close()

    stderr = b"".join(p for s, p in frames if s == host_service.STREAM_STDERR).decode()
    assert "not in allowlist" in stderr
    (_, exit_payload) = next((s, p) for s, p in frames if s == host_service.STREAM_EXIT)
    (rc,) = struct.unpack(">i", exit_payload)
    assert rc == 2


def test_exec_allowlisted_runs_when_allowed(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        session.exec_allowlisted(
            lambda req: ["echo", req["thing"]],
            allowlist={"safe-value"},
        )

    _start_server(handler, socket_path)
    c = _client(socket_path)
    try:
        _send_request(c, {"jail_id": "j", "thing": "safe-value"})
        frames = list(_read_frames(c))
    finally:
        c.close()

    stdout = b"".join(p for s, p in frames if s == host_service.STREAM_STDOUT).decode()
    assert "safe-value" in stdout
    (_, exit_payload) = next((s, p) for s, p in frames if s == host_service.STREAM_EXIT)
    (rc,) = struct.unpack(">i", exit_payload)
    assert rc == 0


def test_socket_permissions_are_600(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        session.exit(0)

    _start_server(handler, socket_path)
    mode = os.stat(socket_path).st_mode & 0o777
    assert mode == 0o600


def test_multiple_connections_served_concurrently(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        time.sleep(0.05)
        session.json({"id": session.request.get("id")})

    _start_server(handler, socket_path)

    results = []
    lock = threading.Lock()

    def client_call(i: int):
        c = _client(socket_path)
        try:
            _send_request(c, {"jail_id": "j", "id": i})
            stdout = b"".join(
                p for s, p in _read_frames(c) if s == host_service.STREAM_STDOUT
            ).decode()
            with lock:
                results.append(json.loads(stdout))
        finally:
            c.close()

    threads = [threading.Thread(target=client_call, args=(i,)) for i in range(5)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - t0

    assert {r["id"] for r in results} == set(range(5))
    # Concurrency lower bound: handler sleeps 50ms; 5 serial calls = 250ms.
    # If truly concurrent, should complete well under 200ms on any machine.
    assert elapsed < 0.45, f"looks serialized: {elapsed:.3f}s"


def test_default_exit_zero_when_handler_returns(socket_path: Path):
    def handler(session: host_service.Session) -> None:
        session.stdout("done\n")
        # No explicit exit call.

    _start_server(handler, socket_path)
    c = _client(socket_path)
    try:
        _send_request(c, {"jail_id": "j"})
        frames = list(_read_frames(c))
    finally:
        c.close()

    (_, exit_payload) = next((s, p) for s, p in frames if s == host_service.STREAM_EXIT)
    (rc,) = struct.unpack(">i", exit_payload)
    assert rc == 0
