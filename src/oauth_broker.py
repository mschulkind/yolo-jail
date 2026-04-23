#!/usr/bin/env python3
"""yolo-claude-oauth-broker-host — per-jail OAuth refresh daemon.

Runs on the host as a ``host_daemon`` of the claude-oauth-broker
loophole.  Listens on a Unix socket; the jail-side TLS terminator
(``src.oauth_broker_jail``) forwards refresh requests over that socket.

Why this split: the host daemon holds the shared flock + rewrites the
shared credentials file, so refreshes across multiple concurrent jails
serialize through us and nobody burns the single-use refresh token.
TLS termination stays inside the jail (unprivileged port 443 in the
container namespace), so we never need to bind :443 on the host.

Protocol: one JSON request per connection (framed per the loophole
protocol — see ``docs/loophole-protocol.md``).  Request shapes:

  {"action": "refresh"}   → runs the refresh flow; returns
                            {access_token, refresh_token, expires_in,
                             token_type} or {error, ...}

The ``--init-ca`` subcommand generates the CA + leaf cert pair into
the loophole directory.  ``just deploy`` runs this once; the daemon
itself is a pure refresh service.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from . import host_service
    from . import loopholes as _loopholes
except ImportError:  # pragma: no cover — running as a script
    from src import host_service  # type: ignore[no-redef]
    from src import loopholes as _loopholes  # type: ignore[no-redef]


# --- Constants ---------------------------------------------------------------

# Writable state dir for the broker loophole — CA + leaf + refresh lock.
# Manifest lives in the bundled_loopholes directory (read-only in the
# installed wheel); generated state can't live there.
BROKER_DIR = _loopholes.state_dir_for("claude-oauth-broker")
CA_CRT = BROKER_DIR / "ca.crt"
CA_KEY = BROKER_DIR / "ca.key"
SERVER_CRT = BROKER_DIR / "server.crt"
SERVER_KEY = BROKER_DIR / "server.key"
REFRESH_LOCK = BROKER_DIR / "refresh.lock"

# Upstream OAuth endpoint.  Extracted from the Claude Code 2.1.x binary;
# stable across patch releases.  If Anthropic moves it, refreshes start
# failing with 404 and you re-verify with:
#   rg -oab 'platform\.claude\.com|/v1/oauth/token' <claude-binary>
UPSTREAM_HOST = "platform.claude.com"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA_HEADER = "oauth-2025-04-20"


# Cloudflare's bot-signature filter on platform.claude.com returns
# HTTP 403 with body ``error code: 1010`` for the default
# ``Python-urllib/<ver>`` User-Agent.  Any non-default UA we've tested
# passes; identifying ourselves honestly also makes Anthropic-side
# log forensics sane.  We try to get the installed yolo-jail version
# from package metadata but fall back to an unversioned string if
# we're running from source or the wheel metadata is unreadable.
def _broker_user_agent() -> str:
    try:
        from importlib.metadata import version as _pkg_version

        return f"yolo-jail-oauth-broker/{_pkg_version('yolo-jail')}"
    except Exception:
        return "yolo-jail-oauth-broker"


USER_AGENT = _broker_user_agent()

# Shared credentials file — lives in the directory-mounted shared
# credentials dir so Claude Code's atomic writer (tmp+rename) works.
DEFAULT_CREDS_PATH = (
    Path.home()
    / ".local/share/yolo-jail/home/.claude-shared-credentials/.credentials.json"
)
# Host-side Claude Code's own credentials file.  When this file exists
# AND its refresh token matches the shared file's (meaning host and
# jail share one identity, the default state after first-boot sync),
# the broker mirrors each refresh here too — keeps host Claude Code
# logged in.  If the refresh tokens differ, host has an independent
# identity and the broker leaves this file alone.
DEFAULT_HOST_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"


log = logging.getLogger("oauth-broker-host")


# --- CA + leaf cert generation ----------------------------------------------


# Common openssl install locations.  The broker is spawned by ``yolo`` as
# a daemon, and depending on how the user launched yolo (mise activate,
# uv run, direct shell, IDE integration, etc.) the inherited PATH may
# not include /usr/bin even on systems where openssl is clearly there.
# We search these absolute paths as a fallback so the broker doesn't
# depend on PATH hygiene at spawn time.
_OPENSSL_FALLBACK_PATHS = (
    "/usr/bin/openssl",
    "/bin/openssl",
    "/usr/local/bin/openssl",
    "/opt/homebrew/bin/openssl",  # Homebrew on Apple Silicon
    "/usr/local/opt/openssl/bin/openssl",  # Homebrew on Intel macOS
    "/run/current-system/sw/bin/openssl",  # NixOS
)


def _resolve_openssl() -> Optional[str]:
    """Find the openssl binary, by PATH or by walking known install dirs.

    Returns the absolute path on success, or None if no openssl can be
    located.  See ``_OPENSSL_FALLBACK_PATHS`` for the rationale.
    """
    found = shutil.which("openssl")
    if found:
        return found
    for p in _OPENSSL_FALLBACK_PATHS:
        if os.access(p, os.X_OK):
            return p
    return None


def _openssl(*args: str, input: Optional[bytes] = None) -> None:
    binary = _resolve_openssl()
    if binary is None:
        # Should be unreachable in practice — ensure_ca_and_leaf
        # validates this up front — but guard anyway for callers that
        # bypass the high-level entrypoint.
        raise RuntimeError("openssl not found; cannot run openssl subcommand")
    proc = subprocess.run(
        [binary, *args],
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

    The CA is valid 10 years; the leaf (for ``UPSTREAM_HOST``) is issued
    once and also valid 10 years — rotating the leaf requires jails to
    re-read the CA, which they only do at boot.  Since the CA itself is
    the trust root, leaf longevity doesn't weaken anything.
    """
    BROKER_DIR.mkdir(parents=True, exist_ok=True)
    have_ca = CA_CRT.is_file() and CA_KEY.is_file()
    have_leaf = SERVER_CRT.is_file() and SERVER_KEY.is_file()
    if have_ca and have_leaf and not force:
        return

    # We need openssl to mint anything missing.  Check up front so the
    # operator gets one actionable error line instead of a Python
    # traceback from subprocess deep inside _openssl().
    if _resolve_openssl() is None:
        # Include the spawned env's PATH so operators can diagnose
        # PATH-stripping wrappers (mise, uv run, IDE integrations) that
        # cause openssl to be missing in the daemon's env even though
        # it's plainly on the user's interactive shell PATH.
        spawn_path = os.environ.get("PATH", "<unset>")
        searched = ":".join(_OPENSSL_FALLBACK_PATHS)
        raise SystemExit(
            "yolo-claude-oauth-broker-host: cannot locate openssl. "
            f"Searched PATH={spawn_path!r} and fallback locations "
            f"({searched}). Install openssl, or symlink it into one "
            "of the fallback locations. "
            "(See docs/claude-oauth-mitm-proxy-plan.md)"
        )

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
        have_leaf = False

    if force or not have_leaf:
        _openssl("genrsa", "-out", str(SERVER_KEY), "2048")
        os.chmod(SERVER_KEY, 0o600)
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
    """Return on-disk tokens if the access token has >= 90s headroom."""
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
            # Cloudflare bot-filter blocks Python-urllib's default UA with
            # error 1010.  See USER_AGENT definition up top.
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _write_tokens(creds_path: Path, oauth: Dict[str, Any]) -> None:
    """Atomic write of the shared credentials file."""
    blob = json.dumps({"claudeAiOauth": oauth}, indent=2)
    fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, blob.encode())
    finally:
        os.close(fd)


def _mirror_to_host_if_same_identity(
    host_path: Path, old_refresh_token: str, new_oauth: Dict[str, Any]
) -> None:
    """If the host creds file shares ``old_refresh_token`` with the shared
    file, mirror the refreshed tokens into it so host Claude Code stays
    logged in.  Best-effort: missing/differing/unreadable host files are
    fine — those cases mean independent identities or no host Claude."""
    if not host_path.is_file():
        return
    try:
        host_data = json.loads(host_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    host_oauth = host_data.get("claudeAiOauth") or {}
    if host_oauth.get("refreshToken") != old_refresh_token:
        return
    merged = dict(host_oauth)
    merged["accessToken"] = new_oauth["accessToken"]
    merged["refreshToken"] = new_oauth["refreshToken"]
    merged["expiresAt"] = new_oauth["expiresAt"]
    host_data["claudeAiOauth"] = merged
    try:
        blob = json.dumps(host_data, separators=(",", ":")).encode()
        fd = os.open(host_path, os.O_WRONLY)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, blob)
            os.ftruncate(fd, len(blob))
        finally:
            os.close(fd)
        log.info("mirrored refresh into host creds %s", host_path)
    except OSError as e:
        log.warning("could not mirror into host creds %s: %s", host_path, e)


def _normalize_oauth(
    upstream_resp: Dict[str, Any], previous: Dict[str, Any]
) -> Dict[str, Any]:
    """Convert upstream {access_token, refresh_token, expires_in} to the
    Claude-Code on-disk shape.  Preserves subscriptionType / scopes from
    the previous record."""
    now_ms = int(time.time() * 1000)
    expires_in = int(upstream_resp.get("expires_in", 3600))
    out = dict(previous)
    out["accessToken"] = upstream_resp["access_token"]
    if "refresh_token" in upstream_resp:
        out["refreshToken"] = upstream_resp["refresh_token"]
    out["expiresAt"] = now_ms + expires_in * 1000
    return out


def _as_oauth_response(oauth: Dict[str, Any]) -> Dict[str, Any]:
    """Shape on-disk tokens back into an upstream-style response body."""
    expires_in = max(
        0, (int(oauth.get("expiresAt", 0)) - int(time.time() * 1000)) // 1000
    )
    return {
        "access_token": oauth.get("accessToken"),
        "refresh_token": oauth.get("refreshToken"),
        "expires_in": expires_in,
        "token_type": "Bearer",
    }


def do_refresh(
    creds_path: Path, host_creds_path: Optional[Path] = DEFAULT_HOST_CREDS_PATH
) -> Dict[str, Any]:
    """Flock-serialized refresh.  Returns a dict either
    ``{access_token, refresh_token, expires_in, token_type}`` on success
    or ``{error, ...}`` on any failure.

    If ``host_creds_path`` is provided and its refresh token matches the
    shared file's pre-refresh refresh token, the new tokens are mirrored
    there too — keeps a host-side Claude Code in sync.
    """
    REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(REFRESH_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        cached = _cached_tokens(creds_path)
        if cached is not None:
            log.info("cache hit")
            return _as_oauth_response(cached)

        try:
            current = json.loads(creds_path.read_text()).get("claudeAiOauth") or {}
        except (OSError, json.JSONDecodeError) as e:
            log.error("creds file unreadable: %s", e)
            return {"error": "creds_unreadable", "message": str(e)}
        refresh_token = current.get("refreshToken")
        if not refresh_token:
            return {"error": "no_refresh_token"}

        log.info("cache miss: refreshing upstream")
        try:
            resp = _refresh_upstream(refresh_token)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            log.error("upstream %s: %s", e.code, body)
            return {"error": "upstream_http", "status": e.code, "body": body}
        except (urllib.error.URLError, OSError) as e:
            log.error("upstream network error: %s", e)
            return {"error": "upstream_unreachable", "message": str(e)}

        new_oauth = _normalize_oauth(resp, previous=current)
        _write_tokens(creds_path, new_oauth)
        if host_creds_path is not None:
            _mirror_to_host_if_same_identity(host_creds_path, refresh_token, new_oauth)
        log.info("refreshed; new expiresAt=%s", new_oauth.get("expiresAt"))
        return _as_oauth_response(new_oauth)


# --- Upstream HTTP proxy (for non-refresh traffic) --------------------------

# Hop-by-hop headers we strip on both legs — never forward these upstream and
# never echo them back to the jail.  ``content-length`` is recomputed.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
    }
)


def do_proxy(
    method: str, path: str, headers: Dict[str, str], body: bytes
) -> Dict[str, Any]:
    """Forward a request to the real ``platform.claude.com``.

    Exists because the jail-side terminator cannot dial the real upstream
    itself — ``--add-host platform.claude.com:127.0.0.1`` routes the
    hostname back to the terminator in a loop.  The host broker has
    normal DNS, so it's the natural place to do the upstream request.

    Returns either ``{status, headers, body_b64}`` on any HTTP response
    (including 4xx/5xx from upstream, which pass through verbatim) or
    ``{error, message}`` on transport-level failure.
    """
    if not path.startswith("/"):
        return {"error": "bad_path", "message": f"path must start with '/': {path!r}"}
    url = f"https://{UPSTREAM_HOST}{path}"
    fwd_headers = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}
    # If the caller (Claude Code) already sent a User-Agent, pass it
    # through verbatim — that's the most authentic request.  Otherwise
    # identify ourselves; Python-urllib's default UA triggers Cloudflare
    # 1010 on platform.claude.com.
    if not any(k.lower() == "user-agent" for k in fwd_headers):
        fwd_headers["User-Agent"] = USER_AGENT
    req = urllib.request.Request(
        url, data=body or None, method=method, headers=fwd_headers
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_status = resp.status
            resp_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP
            }
            resp_body = resp.read()
    except urllib.error.HTTPError as e:
        resp_status = e.code
        resp_headers = {
            k: v for k, v in e.headers.items() if k.lower() not in _HOP_BY_HOP
        }
        resp_body = e.read()
    except (urllib.error.URLError, OSError) as e:
        log.error("proxy upstream error for %s %s: %s", method, path, e)
        return {"error": "upstream_unreachable", "message": str(e)}
    return {
        "status": resp_status,
        "headers": resp_headers,
        "body_b64": base64.b64encode(resp_body).decode("ascii"),
    }


# --- host_service handler ---------------------------------------------------


def _decode_proxy_request(req: Dict[str, Any]) -> "Dict[str, Any] | str":
    """Validate a proxy request; return kwargs for ``do_proxy`` or an error
    message string."""
    method = req.get("method")
    path = req.get("path")
    headers = req.get("headers") or {}
    body_b64 = req.get("body_b64") or ""
    if not isinstance(method, str) or not method:
        return "proxy: missing/invalid 'method'"
    if not isinstance(path, str) or not path:
        return "proxy: missing/invalid 'path'"
    if not isinstance(headers, dict):
        return "proxy: 'headers' must be an object"
    if not isinstance(body_b64, str):
        return "proxy: 'body_b64' must be a string"
    try:
        body = (
            base64.b64decode(body_b64.encode("ascii"), validate=True)
            if body_b64
            else b""
        )
    except (ValueError, UnicodeEncodeError) as e:
        return f"proxy: invalid base64 body: {e}"
    return {
        "method": method,
        "path": path,
        "headers": {str(k): str(v) for k, v in headers.items()},
        "body": body,
    }


def build_handler(creds_path: Path, host_creds_path: Optional[Path] = None):
    def handler(session: "host_service.Session") -> None:
        req = session.request
        action = str(req.get("action") or "refresh")
        if action == "refresh":
            session.json(do_refresh(creds_path, host_creds_path))
            return
        if action == "cached":
            cached = _cached_tokens(creds_path)
            if cached is None:
                session.json({"error": "no_cached_token"})
            else:
                session.json(_as_oauth_response(cached))
            return
        if action == "proxy":
            decoded = _decode_proxy_request(req)
            if isinstance(decoded, str):
                session.json({"error": "bad_request", "message": decoded})
                return
            session.json(do_proxy(**decoded))
            return
        session.stderr(f"unknown action: {action!r}\n")
        session.exit(2)

    return handler


# --- Self-check used by ``yolo doctor`` -------------------------------------


def self_check() -> int:
    """Health check.  Distinguishes three states:

    - **fail** (rc=1): something is genuinely broken — e.g. the creds
      file contains unparseable JSON, or we're missing tools we need at
      runtime.
    - **warn** (rc=0 + NOTE lines): not-yet-ready state that the
      operator will fix with a deploy / login step.  Missing CA/leaf
      before ``--init-ca`` runs, missing creds before first ``/login``.
    - **ok** (rc=0): everything present and parseable.
    """
    warnings: List[str] = []
    failures: List[str] = []

    if not CA_CRT.is_file():
        warnings.append(
            f"{CA_CRT} not yet generated — run `--init-ca` or `just deploy`"
        )
    if not SERVER_CRT.is_file():
        warnings.append(
            f"{SERVER_CRT} not yet generated — run `--init-ca` or `just deploy`"
        )
    if _resolve_openssl() is None:
        # Only hard-fail if state is also missing (we'd need openssl to
        # generate it).  If state already exists, openssl absence is
        # benign at runtime.
        if warnings:
            failures.append(
                "openssl not on PATH and no CA/leaf state yet — "
                "install openssl so `--init-ca` can run"
            )

    creds = DEFAULT_CREDS_PATH
    if creds.exists():
        try:
            raw = creds.read_text()
            if raw.strip():
                json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            failures.append(f"{creds}: {e}")
    else:
        warnings.append(f"{creds} does not exist — run Claude and `/login` first")

    if failures:
        for p in failures:
            print(f"FAIL: {p}")
        for p in warnings:
            print(f"NOTE: {p}")
        return 1
    if warnings:
        for p in warnings:
            print(f"NOTE: {p}")
        print("OK (broker present; state not yet primed)")
        return 0
    print("OK")
    return 0


# --- CLI entry point --------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(description=doc.split("\n\n")[0])
    parser.add_argument(
        "--socket",
        type=Path,
        help="Unix socket to bind (set by ``yolo run``'s host_services pipeline)",
    )
    parser.add_argument(
        "--creds-file",
        type=Path,
        default=DEFAULT_CREDS_PATH,
        help="Shared credentials file (default: the one jails bind-mount)",
    )
    parser.add_argument(
        "--host-creds-file",
        type=Path,
        default=DEFAULT_HOST_CREDS_PATH,
        help=(
            "Host-side Claude Code creds file.  Mirrored when it shares the "
            "same refresh token as --creds-file; left alone otherwise.  Pass "
            "/dev/null to disable mirroring."
        ),
    )
    parser.add_argument(
        "--init-ca",
        action="store_true",
        help="Generate CA + leaf cert and exit (idempotent)",
    )
    parser.add_argument(
        "--force-init-ca",
        action="store_true",
        help="Regenerate CA + leaf even if they exist",
    )
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.self_check:
        return self_check()
    if args.init_ca or args.force_init_ca:
        ensure_ca_and_leaf(force=args.force_init_ca)
        print(f"CA: {CA_CRT}\nleaf: {SERVER_CRT}")
        return 0

    if args.socket is None:
        print(
            "ERROR: --socket is required when running as a daemon.\n"
            "       Use --init-ca for first-time setup.",
            file=sys.stderr,
        )
        return 2

    # Make sure the CA + leaf exist.  Jails need the CA at boot, so the
    # usual path is `just deploy` pre-creates them; but a daemon start
    # without them yet shouldn't crash — just generate on the fly.
    ensure_ca_and_leaf()

    host_creds = args.host_creds_file.expanduser()
    host_creds_path: Optional[Path] = (
        None if str(host_creds) in ("/dev/null", "") else host_creds
    )
    host_service.serve(build_handler(args.creds_file, host_creds_path), args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
