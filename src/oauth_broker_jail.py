#!/usr/bin/env python3
"""yolo-claude-oauth-broker-jail — in-jail TLS terminator for Claude OAuth.

Runs INSIDE the jail as the ``jail_daemon`` of the claude-oauth-broker
loophole.  Claude Code inside the jail opens TLS to
``platform.claude.com``; ``--add-host`` routes that hostname to
``127.0.0.1``, and this daemon terminates the TLS with a leaf cert the
jail trusts (via ``NODE_EXTRA_CA_CERTS``).

For ``POST /v1/oauth/token`` with ``grant_type=refresh_token``:
forward the refresh to the host-side broker over the loophole's Unix
socket (the flock + single-writer lives there).  For any other
``/v1/oauth/token`` grant (``authorization_code`` from ``/login``,
future PKCE exchanges, …) and for any other path: ship the request
over the same socket with ``action=proxy`` and let the host broker
call the real ``platform.claude.com``.  We can't dial the upstream
ourselves — ``--add-host`` maps the hostname back to this daemon, so
a direct ``urllib.urlopen`` loops.  The host has normal DNS, so it
does the upstream call and returns the response bytes to us.

No privileged host ports, no host-side firewall changes, no tampering
with the jail's ambient caps.  Binding :443 inside a container is
unrestricted because the container has its own network namespace and
the daemon runs as UID 0 in that namespace.

Files come from the bind-mounted loophole directory:
``/etc/yolo-jail/loopholes/claude-oauth-broker/``.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import socket
import ssl
import struct
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


LOOPHOLE_DIR = Path("/etc/yolo-jail/loopholes/claude-oauth-broker")
# State dir mount — contains the leaf cert + key generated on the host
# side by ``yolo-claude-oauth-broker-host --init-ca``.
STATE_DIR = Path("/var/lib/yolo-jail/loopholes/claude-oauth-broker")
DEFAULT_CERT = STATE_DIR / "server.crt"
DEFAULT_KEY = STATE_DIR / "server.key"

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


# --- Reverse proxy for non-refresh traffic ----------------------------------
#
# We cannot dial ``platform.claude.com`` from inside the jail directly: the
# ``--add-host`` that makes Claude Code's TLS land here also makes our own
# outbound urllib call resolve back to 127.0.0.1 → us, in a loop.  The host
# broker has normal DNS, so we ship the request over the existing Unix
# socket and let the host do the upstream call.


def _proxy_upstream(
    host_socket_path: str,
    method: str,
    path: str,
    headers: Dict[str, str],
    body: bytes,
) -> Tuple[int, Dict[str, str], bytes]:
    request = {
        "action": "proxy",
        "method": method,
        "path": path,
        "headers": headers,
        "body_b64": base64.b64encode(body).decode("ascii") if body else "",
    }
    try:
        resp = ask_host_broker(host_socket_path, request)
    except RuntimeError as e:
        log.error("proxy via host broker failed: %s", e)
        return (
            502,
            {"Content-Type": "application/json"},
            json.dumps({"error": "broker_unavailable", "detail": str(e)}).encode(),
        )
    if "error" in resp:
        return (
            502,
            {"Content-Type": "application/json"},
            json.dumps(resp).encode(),
        )
    try:
        status = int(resp["status"])
        resp_headers = {str(k): str(v) for k, v in (resp.get("headers") or {}).items()}
        resp_body = base64.b64decode((resp.get("body_b64") or "").encode("ascii"))
    except (KeyError, TypeError, ValueError) as e:
        log.error("malformed proxy response from host broker: %s (resp=%r)", e, resp)
        return (
            502,
            {"Content-Type": "application/json"},
            json.dumps({"error": "broker_bad_response", "detail": str(e)}).encode(),
        )
    return status, resp_headers, resp_body


# --- HTTP handler ------------------------------------------------------------


def _is_refresh_grant(body: bytes) -> bool:
    """True iff ``body`` is a JSON object with
    ``grant_type == "refresh_token"``.

    The broker serializes the single-use refresh-token rotation.
    Anything else that hits ``/v1/oauth/token`` — most importantly
    ``grant_type=authorization_code`` from ``/login`` — must be
    proxied through untouched so the upstream server can mint the
    initial credential.  Unparseable / empty bodies fall through to
    the proxy path too; the upstream will reject them with its own
    (honest) error message instead of the broker returning a
    misleading ``no_refresh_token``.
    """
    if not body:
        return False
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(parsed, dict) and parsed.get("grant_type") == "refresh_token"


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
        ua = self.headers.get("User-Agent", "-")
        is_token_path = self.command == "POST" and self.path.startswith(
            "/v1/oauth/token"
        )
        is_refresh = is_token_path and _is_refresh_grant(body)
        log.info(
            "request: %s %s body_len=%d is_refresh=%s ua=%r",
            self.command,
            self.path,
            len(body),
            is_refresh,
            ua,
        )
        if is_refresh:
            try:
                resp = ask_host_broker(self.host_socket_path, {"action": "refresh"})
            except RuntimeError as e:
                log.error("refresh: host broker error: %s", e)
                self._send(
                    502,
                    json.dumps(
                        {"error": "broker_unavailable", "detail": str(e)}
                    ).encode(),
                )
                return
            if "error" in resp:
                log.warning(
                    "refresh: broker returned error=%s (%s)",
                    resp.get("error"),
                    resp.get("message") or resp.get("body") or "",
                )
                self._send(400, json.dumps(resp).encode())
                return
            log.info(
                "refresh: OK expires_in=%s",
                resp.get("expires_in"),
            )
            self._send(200, json.dumps(resp).encode())
            return

        status, headers, resp_body = _proxy_upstream(
            self.host_socket_path,
            self.command,
            self.path,
            dict(self.headers),
            body,
        )
        log.info(
            "proxy: %s %s -> %d body_len=%d",
            self.command,
            self.path,
            status,
            len(resp_body),
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
