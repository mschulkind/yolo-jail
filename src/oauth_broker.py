#!/usr/bin/env python3
"""yolo-claude-oauth-broker — MITM proxy that serializes Claude OAuth refreshes.

Runs on the host.  Jails route ``platform.claude.com`` to this daemon via a
``--add-host`` entry + a bundled CA trusted through ``NODE_EXTRA_CA_CERTS``.
When a jail tries to refresh its OAuth token, the request lands here instead
of Anthropic.  The broker:

1. Acquires an flock so only one refresh is in flight per host.
2. Checks the shared credentials file; if its access token is still good,
   returns those tokens — no upstream call, no race.
3. Otherwise, forwards the refresh to real ``platform.claude.com``, writes
   the new tokens to the shared file via in-place truncate+write, and
   returns them.
4. For any non-OAuth path, reverse-proxies to real ``platform.claude.com``
   so the ``/login`` flow keeps working.

Why this exists
---------------
Anthropic's OAuth server uses single-use refresh tokens.  When two jails
notice an expiring access token in the same window, both hit the refresh
endpoint; the second one loses (401) and forces the user to ``/login``.
``scripts/claude-token-refresher`` (the simple refresher) mostly prevents
this, but jails still refresh on their own if they've cached the old token
in memory at process start.  This broker eliminates races entirely: jails
can't refresh independently because they can't reach Anthropic directly.

See ``docs/claude-oauth-mitm-proxy-plan.md`` for the full design.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from . import claude_refresher as refresher
except ImportError:  # pragma: no cover — running as a script
    from src import claude_refresher as refresher  # type: ignore[no-redef]


# --- Constants ---------------------------------------------------------------

BROKER_DIR = (
    Path.home() / ".local" / "share" / "yolo-jail" / "loopholes" / "claude-oauth-broker"
)
CA_CRT = BROKER_DIR / "ca.crt"
CA_KEY = BROKER_DIR / "ca.key"
SERVER_CRT = BROKER_DIR / "server.crt"
SERVER_KEY = BROKER_DIR / "server.key"
REFRESH_LOCK = BROKER_DIR / "refresh.lock"

UPSTREAM_HOST = "platform.claude.com"
DEFAULT_LISTEN_HOST = "0.0.0.0"
DEFAULT_LISTEN_PORT = 8443

# Same endpoint as the standalone refresher.  Kept in sync there.
TOKEN_URL = refresher.TOKEN_URL
CLIENT_ID = refresher.CLIENT_ID
OAUTH_BETA_HEADER = refresher.OAUTH_BETA_HEADER


log = logging.getLogger("oauth-broker")


# --- CA + leaf cert generation ----------------------------------------------


def _openssl(*args: str, input: Optional[bytes] = None) -> None:
    """Run openssl with capture; raise on failure."""
    proc = subprocess.run(
        ["openssl", *args],
        input=input,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"openssl {' '.join(args)} failed:\n{proc.stderr.decode(errors='replace')}"
        )


def ensure_ca_and_leaf(force: bool = False) -> None:
    """Create the CA + leaf cert pair on first run.  Idempotent.

    The CA is valid 10 years; the leaf (for UPSTREAM_HOST) is issued once and
    also valid 10 years — we don't rotate leaves because rotation would
    require jails to re-read the CA, which they only do at boot.  Since the
    CA itself is the trust root, leaf longevity doesn't weaken anything.
    """
    BROKER_DIR.mkdir(parents=True, exist_ok=True)

    have_ca = CA_CRT.is_file() and CA_KEY.is_file()
    have_leaf = SERVER_CRT.is_file() and SERVER_KEY.is_file()
    if have_ca and have_leaf and not force:
        return

    if force or not have_ca:
        _openssl("genrsa", "-out", str(CA_KEY), "4096")
        os.chmod(CA_KEY, 0o600)
        _openssl(
            "req",
            "-x509",
            "-new",
            "-nodes",
            "-key",
            str(CA_KEY),
            "-sha256",
            "-days",
            "3650",
            "-out",
            str(CA_CRT),
            "-subj",
            "/CN=yolo-jail-claude-oauth-broker/O=yolo-jail/OU=local",
        )
        # Leaf was signed by the old CA — invalidate it.
        have_leaf = False

    if force or not have_leaf:
        _openssl("genrsa", "-out", str(SERVER_KEY), "2048")
        os.chmod(SERVER_KEY, 0o600)
        # Use a CSR with SAN extension.  openssl req doesn't take SANs on the
        # command line directly without a config, so we build a minimal one.
        cfg = (
            "[req]\n"
            "distinguished_name=req_distinguished_name\n"
            "req_extensions=v3_req\n"
            "prompt=no\n"
            "[req_distinguished_name]\n"
            f"CN={UPSTREAM_HOST}\n"
            "[v3_req]\n"
            f"subjectAltName=DNS:{UPSTREAM_HOST},DNS:localhost\n"
        )
        cfg_path = BROKER_DIR / "leaf.cnf"
        cfg_path.write_text(cfg)
        csr_path = BROKER_DIR / "server.csr"
        _openssl(
            "req",
            "-new",
            "-key",
            str(SERVER_KEY),
            "-out",
            str(csr_path),
            "-config",
            str(cfg_path),
        )
        _openssl(
            "x509",
            "-req",
            "-in",
            str(csr_path),
            "-CA",
            str(CA_CRT),
            "-CAkey",
            str(CA_KEY),
            "-CAcreateserial",
            "-out",
            str(SERVER_CRT),
            "-days",
            "3650",
            "-sha256",
            "-extfile",
            str(cfg_path),
            "-extensions",
            "v3_req",
        )
        csr_path.unlink(missing_ok=True)


# --- Cached-token + flock refresh ------------------------------------------


def _cached_tokens(creds_path: Path) -> Optional[Dict[str, Any]]:
    """Return the on-disk tokens if the access token has useful headroom.

    "Useful" = at least 90 seconds left.  A jail that gets these tokens has
    to live with them until they expire on its own; we don't want to hand
    back a token that will expire mid-conversation.
    """
    try:
        data = json.loads(creds_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    oauth = data.get("claudeAiOauth") or {}
    expires_at_ms = int(oauth.get("expiresAt", 0))
    if expires_at_ms - int(time.time() * 1000) < 90_000:
        return None
    return oauth


def _refresh_upstream(refresh_token: str) -> Dict[str, Any]:
    """Hit the real upstream to rotate tokens.  Returns the parsed response."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "anthropic-beta": OAUTH_BETA_HEADER,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _write_tokens(creds_path: Path, oauth: Dict[str, Any]) -> None:
    """In-place truncate+write — preserves the bind-mount inode the jails see."""
    blob = json.dumps({"claudeAiOauth": oauth}, indent=2)
    fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob.encode())
    finally:
        os.close(fd)


def handle_refresh(creds_path: Path) -> Tuple[int, bytes]:
    """Return (HTTP status, JSON body) for a refresh request.

    Holds a file lock for the duration so two concurrent refreshes from
    different jails serialize through us, not through Anthropic's
    refresh-token rotation (which would invalidate the loser).
    """
    REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(REFRESH_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        cached = _cached_tokens(creds_path)
        if cached is not None:
            log.info("cache hit: returning existing access token")
            return 200, json.dumps(_as_oauth_response(cached)).encode()

        try:
            current = json.loads(creds_path.read_text()).get("claudeAiOauth") or {}
        except (OSError, json.JSONDecodeError) as e:
            log.error("creds file unreadable: %s", e)
            return 500, json.dumps({"error": "creds_unreadable"}).encode()
        refresh_token = current.get("refreshToken")
        if not refresh_token:
            return 400, json.dumps({"error": "no_refresh_token"}).encode()

        log.info("cache miss: refreshing upstream")
        try:
            resp = _refresh_upstream(refresh_token)
        except urllib.error.HTTPError as e:
            body = e.read()
            log.error("upstream %s: %s", e.code, body[:200])
            return e.code, body
        except (urllib.error.URLError, OSError) as e:
            log.error("upstream network error: %s", e)
            return 502, json.dumps({"error": "upstream_unreachable"}).encode()

        new_oauth = _normalize_oauth(resp, previous=current)
        _write_tokens(creds_path, new_oauth)
        log.info("refreshed; new expiresAt=%s", new_oauth.get("expiresAt"))
        return 200, json.dumps(_as_oauth_response(new_oauth)).encode()


def _normalize_oauth(
    upstream_resp: Dict[str, Any], previous: Dict[str, Any]
) -> Dict[str, Any]:
    """Convert the upstream ``{access_token, refresh_token, expires_in}`` into
    the Claude-Code-on-disk shape ``{accessToken, refreshToken, expiresAt}``.

    Preserves ``subscriptionType`` and ``scopes`` from the previous record —
    upstream doesn't return them on refresh and Claude Code reads them.
    """
    now_ms = int(time.time() * 1000)
    expires_in = int(upstream_resp.get("expires_in", 3600))
    out = dict(previous)
    out["accessToken"] = upstream_resp["access_token"]
    if "refresh_token" in upstream_resp:
        out["refreshToken"] = upstream_resp["refresh_token"]
    out["expiresAt"] = now_ms + expires_in * 1000
    return out


def _as_oauth_response(oauth: Dict[str, Any]) -> Dict[str, Any]:
    """Shape our on-disk tokens back into an upstream-style response body.

    Claude Code parses the refresh response into its on-disk form via fields
    named ``access_token``, ``refresh_token``, ``expires_in``.  We emit those.
    """
    expires_in = max(
        0, (int(oauth.get("expiresAt", 0)) - int(time.time() * 1000)) // 1000
    )
    return {
        "access_token": oauth.get("accessToken"),
        "refresh_token": oauth.get("refreshToken"),
        "expires_in": expires_in,
        "token_type": "Bearer",
    }


# --- Reverse proxy for non-OAuth paths --------------------------------------


def _proxy_upstream(
    method: str, path: str, headers: Dict[str, str], body: bytes
) -> Tuple[int, Dict[str, str], bytes]:
    url = f"https://{UPSTREAM_HOST}{path}"
    # Strip hop-by-hop + Host so urllib sets its own.
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
            resp_body = resp.read()
            resp_headers = {k: v for k, v in resp.headers.items()}
            return resp.status, resp_headers, resp_body
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, {"Content-Type": "application/json"}, body
    except (urllib.error.URLError, OSError) as e:
        log.error("proxy upstream error: %s", e)
        return (
            502,
            {"Content-Type": "application/json"},
            json.dumps({"error": "upstream_unreachable"}).encode(),
        )


# --- HTTP handler ------------------------------------------------------------


class BrokerHandler(BaseHTTPRequestHandler):
    creds_path: Path = Path()  # populated per-server via factory

    # Silence default request logging — we use our own logger.
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
                continue  # we set our own
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
            status, resp_body = handle_refresh(self.creds_path)
            self._send(status, resp_body)
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


def make_server(host: str, port: int, creds_path: Path) -> ThreadingHTTPServer:
    """Build an HTTPS server bound to (host, port) with our CA-issued leaf."""
    handler_cls = type("_Handler", (BrokerHandler,), {"creds_path": creds_path})
    server = ThreadingHTTPServer((host, port), handler_cls)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(SERVER_CRT), keyfile=str(SERVER_KEY))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


# --- Self-check (used by `yolo doctor` via manifest.doctor_cmd) -------------

SYSTEMD_SERVICE = "claude-oauth-broker.service"


def _files_check() -> List[str]:
    problems: List[str] = []
    if not CA_CRT.is_file():
        problems.append(f"missing {CA_CRT}")
    if not SERVER_CRT.is_file():
        problems.append(f"missing {SERVER_CRT}")
    creds = refresher.DEFAULT_CREDS_PATH
    if creds.exists():
        try:
            json.loads(creds.read_text())
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"{creds}: {e}")
    else:
        problems.append(f"{creds} does not exist (no one has /login'd yet)")
    return problems


def _systemd_check() -> List[str]:
    """Verify the systemd user service is running.  Diagnoses the common
    port-443 privilege failure modes when the service isn't active.

    Silent (returns ``[]``) when systemctl is unavailable — macOS / launchd
    users don't have a service unit to check, and the TCP probe below is
    still meaningful on its own.
    """
    if not shutil.which("systemctl"):
        return []
    # Does the unit even exist?  If the user hasn't run `just deploy` yet,
    # there's nothing to check — not a failure, just not installed.
    exists = subprocess.run(
        ["systemctl", "--user", "cat", SYSTEMD_SERVICE],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if exists.returncode != 0:
        return []
    active = subprocess.run(
        ["systemctl", "--user", "is-active", SYSTEMD_SERVICE],
        capture_output=True,
        text=True,
        timeout=5,
    )
    state = (active.stdout or active.stderr).strip() or "unknown"
    if state == "active":
        return []

    # Inactive — scan the journal for known failure patterns and offer
    # specific remediation.  Generic fallback otherwise.
    journal = subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            SYSTEMD_SERVICE,
            "-n",
            "40",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout

    low = journal.lower()
    if "permission denied" in low and (
        "bind" in low or "cap_net_bind" in low or ":443" in journal
    ):
        return [
            "\n".join(
                [
                    f"{SYSTEMD_SERVICE} state={state}: port 443 bind denied.",
                    "AmbientCapabilities=CAP_NET_BIND_SERVICE isn't effective on this host.",
                    "Pick one:",
                    "  (1) sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0",
                    "      (persist in /etc/sysctl.d/99-yolo-jail.conf)",
                    "  (2) edit the unit's ExecStart to --port 8443,",
                    "      then add an iptables DNAT from :443 → :8443 on the bridge iface",
                    "  (3) switch to a runtime that honors the unit's ambient caps",
                    "See loopholes/claude-oauth-broker/README.md.",
                ]
            )
        ]
    if "address already in use" in low:
        return [
            "\n".join(
                [
                    f"{SYSTEMD_SERVICE} state={state}: port 443 already bound.",
                    "Identify the holder: `ss -tlnp sport = :443`",
                    "(often the host's own web server or a prior broker instance).",
                ]
            )
        ]
    if "no such file" in low and "openssl" in low:
        return [
            "\n".join(
                [
                    f"{SYSTEMD_SERVICE} state={state}: openssl not found.",
                    "Install it on the host — only needed once, for CA generation.",
                ]
            )
        ]
    if "exec format error" in low or "failed to execute" in low:
        return [
            "\n".join(
                [
                    f"{SYSTEMD_SERVICE} state={state}: ExecStart binary missing or stale.",
                    "Re-run `just deploy` to reinstall.",
                ]
            )
        ]
    return [
        "\n".join(
            [
                f"{SYSTEMD_SERVICE} state={state}.",
                f"journalctl --user -u {SYSTEMD_SERVICE} -n 50 --no-pager",
            ]
        )
    ]


def _tcp_probe(host: str, port: int, timeout: float = 2.0) -> List[str]:
    """TCP connect to the broker's advertised endpoint.

    This is what a jail would do; if it fails, the jail will get 401s on
    every refresh attempt regardless of what systemd says.  We connect
    raw TCP (no TLS handshake) so we don't need to drag the CA into the
    probe path — the bind itself is what we're verifying.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
    except OSError as e:
        return [
            "\n".join(
                [
                    f"cannot reach broker at {host}:{port}: {e}.",
                    "The daemon is not bound to the address jails route",
                    "platform.claude.com to.  Check the service logs.",
                ]
            )
        ]
    finally:
        s.close()
    return []


def self_check(probe_host: str = "127.0.0.1", probe_port: int = 443) -> int:
    """Aggregate health check used by ``yolo doctor``.

    Order of checks is cheap → expensive: file-level (ms), systemd query
    (~50ms), TCP probe (up to ``timeout``).  Failures short-circuit the
    message but not the checks — we report everything wrong so the operator
    fixes once, not per ``yolo doctor`` run.
    """
    problems = _files_check() + _systemd_check() + _tcp_probe(probe_host, probe_port)
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print("OK")
    return 0


# --- CLI entry point --------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.split("\n\n")[0])
    parser.add_argument("--host", default=DEFAULT_LISTEN_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    parser.add_argument(
        "--creds-file",
        type=Path,
        default=refresher.DEFAULT_CREDS_PATH,
        help="Shared credentials file (default: the one jails bind-mount)",
    )
    parser.add_argument(
        "--init-ca",
        action="store_true",
        help="Generate CA + leaf cert and exit (idempotent)",
    )
    parser.add_argument(
        "--force-init-ca",
        action="store_true",
        help="Regenerate CA + leaf even if they exist (invalidates trust — rerun `just deploy`)",
    )
    parser.add_argument(
        "--self-check", action="store_true", help="Emit status and exit"
    )
    parser.add_argument(
        "--probe-host",
        default="127.0.0.1",
        help=(
            "Address self-check TCP-probes. Default: 127.0.0.1 — the broker binds "
            "to 0.0.0.0:443, and container-origin traffic is routed to the host's "
            "loopback via --add-host :host-gateway, so loopback sees the same "
            "socket the jail does."
        ),
    )
    parser.add_argument(
        "--probe-port",
        type=int,
        default=443,
        help="Port self-check TCP-probes (default: 443 — the port jails connect to)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.self_check:
        return self_check(probe_host=args.probe_host, probe_port=args.probe_port)

    if args.init_ca or args.force_init_ca:
        ensure_ca_and_leaf(force=args.force_init_ca)
        print(f"CA: {CA_CRT}\nleaf: {SERVER_CRT}")
        return 0

    ensure_ca_and_leaf()
    server = make_server(args.host, args.port, args.creds_file)
    log.info(
        "listening on https://%s:%d (intercepting %s)",
        args.host,
        args.port,
        UPSTREAM_HOST,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
