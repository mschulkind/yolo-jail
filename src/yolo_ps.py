#!/usr/bin/env python3
"""yolo-ps — jail-side CLI for the host-processes loophole.

Runs inside the jail, talks over the Unix socket the host-side daemon
exposes.  Output is JSON by default (agents parse JSON); ``--table``
pretty-prints for humans.

The socket path is passed via ``YOLO_HOST_PROCESSES_SOCKET`` — set by
``yolo run`` when the loophole is active.  If that env var is missing,
the loophole isn't wired up for this jail (likely not enabled in
``yolo-jail.jsonc``).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
from typing import Any, Dict, Optional


STREAM_STDOUT = 0
STREAM_STDERR = 1
STREAM_EXIT = 2


def _send_request(conn: socket.socket, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    conn.sendall(struct.pack(">I", len(body)))
    conn.sendall(body)


def _recv_all(conn: socket.socket, n: int) -> "Optional[bytes]":
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _stream_response(conn: socket.socket) -> int:
    """Read framed response, forward stdout/stderr, return exit code."""
    while True:
        header = _recv_all(conn, 5)
        if header is None:
            return 1
        stream_id, length = struct.unpack(">BI", header)
        payload = _recv_all(conn, length) if length else b""
        if payload is None:
            return 1
        if stream_id == STREAM_STDOUT:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        elif stream_id == STREAM_STDERR:
            sys.stderr.buffer.write(payload)
            sys.stderr.buffer.flush()
        elif stream_id == STREAM_EXIT:
            (rc,) = struct.unpack(">i", payload)
            return rc
        else:
            # Unknown stream — ignore, keep reading.
            continue


def _call(socket_path: str, request: Dict[str, Any]) -> int:
    """One request/response round trip.  Returns daemon exit code."""
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(socket_path)
    except OSError as e:
        print(
            f"yolo-ps: cannot reach loophole socket {socket_path}: {e}", file=sys.stderr
        )
        return 2
    try:
        jail_id = os.environ.get("YOLO_JAIL_ID") or os.environ.get(
            "HOSTNAME", "unknown"
        )
        _send_request(conn, {"jail_id": jail_id, **request})
        return _stream_response(conn)
    finally:
        conn.close()


def _resolve_socket() -> Optional[str]:
    return os.environ.get("YOLO_HOST_PROCESSES_SOCKET")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query host processes via the yolo-jail host-processes loophole.",
        epilog=(
            "By default emits framed ps output from the host daemon as-is.  "
            "Use --json for a parsed structured result (when the daemon supports it)."
        ),
    )
    parser.add_argument(
        "-t",
        "--tree",
        action="store_true",
        help="pstree-style output, filtered to allowlisted comms + their children",
    )
    parser.add_argument(
        "--pid",
        type=int,
        help="Details for a single PID (rejected if its comm isn't allowlisted)",
    )
    parser.add_argument(
        "--socket",
        help="Override socket path (default: $YOLO_HOST_PROCESSES_SOCKET)",
    )
    args = parser.parse_args(argv)

    sock = args.socket or _resolve_socket()
    if not sock:
        print(
            "yolo-ps: no socket.  The host-processes loophole isn't wired "
            "up in this jail.  Add `host_processes.visible: [...]` to your "
            "yolo-jail.jsonc and restart the jail.",
            file=sys.stderr,
        )
        return 2

    if args.pid is not None:
        request: Dict[str, Any] = {"mode": "pid", "pid": args.pid}
    elif args.tree:
        request = {"mode": "tree"}
    else:
        request = {"mode": "list"}

    return _call(sock, request)


if __name__ == "__main__":
    sys.exit(main())
