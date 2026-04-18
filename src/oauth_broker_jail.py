#!/usr/bin/env python3
"""yolo-claude-oauth-broker-jail — in-jail TLS terminator for Claude OAuth.

Runs INSIDE the jail as the ``jail_daemon`` of the claude-oauth-broker
loophole.  Claude Code inside the jail opens TLS to
``platform.claude.com``; ``--add-host`` routes that hostname to
``127.0.0.1``, and this daemon terminates the TLS with a leaf cert the
jail trusts (via ``NODE_EXTRA_CA_CERTS``).

For ``POST /v1/oauth/token``: forward the refresh request to the
host-side broker over the loophole's Unix socket (the flock +
single-writer lives there).  For any other path: reverse-proxy to the
real ``platform.claude.com`` so ``/login`` and any future endpoints
keep working.

No privileged host ports, no host-side firewall changes, no tampering
with the jail's ambient caps.  Binding :443 inside a container is
unrestricted because the container has its own network namespace and
the daemon runs as UID 0 in that namespace.

Files come from the bind-mounted loophole directory:
``/etc/yolo-jail/loopholes/claude-oauth-broker/``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


LOOPHOLE_DIR = Path("/etc/yolo-jail/loopholes/claude-oauth-broker")
DEFAULT_CERT = LOOPHOLE_DIR / "server.crt"
DEFAULT_KEY = LOOPHOLE_DIR / "server.key"

UPSTREAM_HOST = "platform.claude.com"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 443


log = logging.getLogger("oauth-broker-jail")


# --- Frame protocol client for the host-side loophole ----------------------


STREAM_STDOUT = 0
STREAM_STDERR = 1
STREAM_EXIT = 2


def _recv_all(conn: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def ask_host_broker(socket_path: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Send a request to the host-side OAuth broker over its Unix socket,
    return the parsed JSON response.  Raises ``RuntimeError`` on protocol
    errors — caller translates those into 502s for Claude."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(30.0)
    try:
        conn.connect(socket_path)
        body = json.dumps(request).encode()
        conn.sendall(struct.pack(">I", len(body)))
        conn.sendall(body)
        stdout = bytearray()
        rc: Optional[int] = None
        while True:
            header = _recv_all(conn, 5)
            if header is None:
                break
            stream_id, length = struct.unpack(">BI", header)
            payload = _recv_all(conn, length) if length else b""
            if payload is None:
                break
            if stream_id == STREAM_STDOUT:
                stdout.extend(payload)
            elif stream_id == STREAM_STDERR:
                log.warning(
                    "host broker stderr: %s", payload.decode(errors="replace").strip()
                )
            elif stream_id == STREAM_EXIT:
                (rc,) = struct.unpack(">i", payload)
                break
    finally:
        conn.close()
    if rc is None:
        raise RuntimeError("host broker closed without an exit frame")
    if rc != 0:
        raise RuntimeError(f"host broker exited {rc}")
    try:
        return json.loads(stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise RuntimeError(f"host broker returned non-JSON: {e}") from e


# --- Reverse proxy for non-OAuth paths --------------------------------------


def _proxy_upstream(
    method: str, path: str, headers: Dict[str, str], body: bytes
) -> Tuple[int, Dict[str, str], bytes]:
    url = f"https://{UPSTREAM_HOST}{path}"
    fwd_headers = {
        k: v
        for k, v in headers.items()
        if k.lower() not in {"host", "connection", "content-length"}
    }
    req = urllib.request.Request(
        url, data=body or None, method=method, headers=fwd_headers
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k: v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {"Content-Type": "application/json"}, e.read()
    except (urllib.error.URLError, OSError) as e:
        log.error("proxy upstream error: %s", e)
        return (
            502,
            {"Content-Type": "application/json"},
            json.dumps({"error": "upstream_unreachable"}).encode(),
        )


# --- HTTP handler ------------------------------------------------------------


class _JailBrokerHandler(BaseHTTPRequestHandler):
    host_socket_path: str = ""  # populated per-server

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s - %s", self.address_string(), format % args)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(length) if length else b""

    def _send(
        self, status: int, body: bytes, headers: Optional[Dict[str, str]] = None
    ) -> None:
        self.send_response(status)
        sent_ct = False
        for k, v in (headers or {}).items():
            if k.lower() == "content-length":
                continue
            if k.lower() == "content-type":
                sent_ct = True
            self.send_header(k, v)
        if not sent_ct:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        body = self._read_body()
        if self.command == "POST" and self.path.startswith("/v1/oauth/token"):
            try:
                resp = ask_host_broker(self.host_socket_path, {"action": "refresh"})
            except RuntimeError as e:
                log.error("host broker error: %s", e)
                self._send(
                    502,
                    json.dumps(
                        {"error": "broker_unavailable", "detail": str(e)}
                    ).encode(),
                )
                return
            if "error" in resp:
                self._send(400, json.dumps(resp).encode())
                return
            self._send(200, json.dumps(resp).encode())
            return

        status, headers, resp_body = _proxy_upstream(
            self.command, self.path, dict(self.headers), body
        )
        self._send(status, resp_body, headers)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_PATCH = _handle


def make_server(
    host: str, port: int, cert: Path, key: Path, host_socket_path: str
) -> ThreadingHTTPServer:
    handler_cls = type(
        "_Handler", (_JailBrokerHandler,), {"host_socket_path": host_socket_path}
    )
    server = ThreadingHTTPServer((host, port), handler_cls)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


# --- CLI entry point --------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.split("\n\n")[0])
    parser.add_argument("--host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument(
        "--cert",
        type=Path,
        default=DEFAULT_CERT,
        help=f"TLS cert (default: {DEFAULT_CERT})",
    )
    parser.add_argument(
        "--key",
        type=Path,
        default=DEFAULT_KEY,
        help=f"TLS key (default: {DEFAULT_KEY})",
    )
    parser.add_argument(
        "--host-socket",
        default=os.environ.get("YOLO_SERVICE_CLAUDE_OAUTH_BROKER_SOCKET", ""),
        help="Unix socket for the host-side broker (default: from env)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.host_socket:
        log.error(
            "no host socket path available — expected YOLO_SERVICE_CLAUDE_OAUTH_BROKER_SOCKET"
        )
        return 2
    if not args.cert.is_file() or not args.key.is_file():
        log.error(
            "missing %s or %s — did `just deploy` run --init-ca?",
            args.cert,
            args.key,
        )
        return 2

    server = make_server(args.host, args.port, args.cert, args.key, args.host_socket)
    log.info(
        "listening on https://%s:%d (intercepting %s → %s)",
        args.host,
        args.port,
        UPSTREAM_HOST,
        args.host_socket,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
