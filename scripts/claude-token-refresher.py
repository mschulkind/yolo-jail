#!/usr/bin/env python3
"""
claude-token-refresher — periodic Claude Code OAuth token refresher.

Runs on the host (not inside a jail). Refreshes the Claude Code OAuth access
token well before it expires, so jails never need to refresh on their own.
This sidesteps the Anthropic refresh-token rotation race that causes jails to
randomly get logged out when two of them try to refresh at the same time.

Architecture
------------
- One shared .credentials.json file on the host, bind-mounted into all jails.
- This script, running from a systemd timer / cron, is the only entity that
  ever talks to the Anthropic token endpoint.
- Jails read the file. When their in-memory access token is about to expire,
  Claude Code calls `ak4()` which re-reads the file before hitting the
  network; if a fresh token is on disk, it's used and no network refresh
  happens. No race, no 401.

Atomic writes
-------------
The file is the bind-mount *source* — jails hold the same inode open via the
bind mount. A tmp+rename on the host would create a new inode that jails
wouldn't see (they'd see the old inode forever, or until they restart). So
we use in-place truncate+write under flock. The write is a single syscall for
the full JSON blob (~500 bytes), which the kernel delivers atomically to
concurrent readers on the same inode.

Usage
-----
    # Dry run — check state and print what would happen, no network calls:
    claude-token-refresher --dry-run

    # Real refresh, only if the access token expires within the threshold:
    claude-token-refresher

    # Force a refresh regardless of expiry (use sparingly — burns a refresh
    # token, could race any running Claude Code):
    claude-token-refresher --force

    # Custom credentials file location (default is the yolo-jail shared path):
    claude-token-refresher --creds-file ~/.claude/.credentials.json

Exit codes
----------
    0  success (refreshed, or no refresh needed, or dry-run)
    1  transient failure (network error, non-2xx from upstream)
    2  permanent failure (credentials file missing/corrupt, no refresh token)
    3  lock contention (another refresher is running)

Security
--------
Never logs token values. Logs token prefixes (first 12 chars) for tracing.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Constants extracted from the Claude Code 2.1.101 binary. Stable in 2.1.x.
# If Anthropic moves the endpoint, this script will start failing with 404 and
# you'll need to re-verify from the current binary:
#   rg -oab 'platform\.claude\.com|/v1/oauth/token' <claude-binary>
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Default path: the yolo-jail shared credentials file. Jails bind-mount this
# file, so in-place writes here propagate to every running jail.
DEFAULT_CREDS_PATH = (
    Path.home() / ".local/share/yolo-jail/home/.claude/.credentials.json"
)
DEFAULT_LOCK_PATH = Path.home() / ".local/share/yolo-jail/oauth-broker/refresh.lock"

# Refresh when the access token has less than this much time left.
# Access tokens typically live ~8 hours, so 30 minutes gives plenty of headroom
# and still means the script only actually refreshes once per hour-ish in a
# tight cron loop.
DEFAULT_REFRESH_THRESHOLD_SECS = 30 * 60

log = logging.getLogger("claude-token-refresher")


@dataclass
class Credentials:
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: list[str]
    subscription_type: str | None
    rate_limit_tier: str | None
    raw: dict[str, Any]  # the full parsed file, so we can round-trip unknown fields

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        text = path.read_text()
        data = json.loads(text)
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            raise ValueError("credentials file missing 'claudeAiOauth' object")
        for field in ("accessToken", "refreshToken", "expiresAt"):
            if field not in oauth:
                raise ValueError(f"credentials file missing '{field}'")
        return cls(
            access_token=str(oauth["accessToken"]),
            refresh_token=str(oauth["refreshToken"]),
            expires_at_ms=int(oauth["expiresAt"]),
            scopes=list(oauth.get("scopes", [])),
            subscription_type=oauth.get("subscriptionType"),
            rate_limit_tier=oauth.get("rateLimitTier"),
            raw=data,
        )

    def seconds_until_expiry(self) -> float:
        return self.expires_at_ms / 1000.0 - time.time()

    def needs_refresh(self, threshold_secs: float) -> bool:
        return self.seconds_until_expiry() < threshold_secs

    def with_new_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_at_ms: int,
        scopes: list[str] | None,
    ) -> "Credentials":
        new_raw = json.loads(json.dumps(self.raw))  # deep copy
        oauth = new_raw["claudeAiOauth"]
        oauth["accessToken"] = access_token
        oauth["refreshToken"] = refresh_token
        oauth["expiresAt"] = expires_at_ms
        if scopes is not None:
            oauth["scopes"] = scopes
        return Credentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
            scopes=scopes if scopes is not None else self.scopes,
            subscription_type=self.subscription_type,
            rate_limit_tier=self.rate_limit_tier,
            raw=new_raw,
        )

    def serialize(self) -> bytes:
        # Match Claude Code's own format: compact, single line, no trailing
        # newline. Preserves exact structure so ak4()'s mtime-based cache
        # reload sees a valid file.
        return json.dumps(self.raw, separators=(",", ":")).encode("utf-8")


def fmt_token_prefix(token: str) -> str:
    """Return a short non-sensitive prefix for logging."""
    return token[:12] + "…" if len(token) > 12 else token


def fmt_expiry(creds: Credentials) -> str:
    secs = creds.seconds_until_expiry()
    if secs < 0:
        return f"expired {-secs:.0f}s ago"
    if secs < 120:
        return f"expires in {secs:.0f}s"
    if secs < 7200:
        return f"expires in {secs / 60:.1f}m"
    return f"expires in {secs / 3600:.1f}h"


def acquire_lock(lock_path: Path, wait_secs: float = 0.0):
    """Acquire an exclusive flock. Returns the file handle; caller must keep it
    alive for the lock to hold. Raises BlockingIOError on contention.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        if wait_secs > 0:
            deadline = time.monotonic() + wait_secs
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)
        else:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        fh.close()
        raise
    return fh


def call_refresh_endpoint(refresh_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """POST to the Anthropic token endpoint and return the parsed JSON body.
    Raises urllib.error.HTTPError / URLError on non-2xx / network failure.
    """
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "claude-token-refresher/1.0",
            "anthropic-beta": OAUTH_BETA_HEADER,
        },
    )

    log.debug(
        "POST %s (client_id=%s, refresh_token=%s)",
        TOKEN_URL,
        CLIENT_ID,
        fmt_token_prefix(refresh_token),
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode("utf-8"))


def parse_refresh_response(
    resp: dict[str, Any],
) -> tuple[str, str, int, list[str] | None]:
    """Extract (access_token, refresh_token, expires_at_ms, scopes) from a
    token endpoint response. Handles both `expires_at` (absolute ms) and
    `expires_in` (seconds from now) formats.
    """
    access = resp.get("access_token")
    refresh = resp.get("refresh_token")
    if not access or not refresh:
        raise ValueError(f"refresh response missing tokens: keys={list(resp.keys())}")

    # Prefer absolute expiry if present; fall back to expires_in.
    if "expires_at" in resp:
        expires_at_ms = int(resp["expires_at"])
        # Some endpoints return seconds, others milliseconds. Heuristic: if the
        # value is smaller than 10^12, it's seconds.
        if expires_at_ms < 10**12:
            expires_at_ms *= 1000
    elif "expires_in" in resp:
        expires_at_ms = int(time.time() * 1000) + int(resp["expires_in"]) * 1000
    else:
        raise ValueError("refresh response missing expires_at/expires_in")

    scopes_raw = resp.get("scope") or resp.get("scopes")
    scopes: list[str] | None
    if isinstance(scopes_raw, str):
        scopes = scopes_raw.split()
    elif isinstance(scopes_raw, list):
        scopes = [str(s) for s in scopes_raw]
    else:
        scopes = None

    return str(access), str(refresh), expires_at_ms, scopes


def write_credentials_in_place(path: Path, creds: Credentials) -> None:
    """Overwrite the credentials file in-place (no rename).

    Why no rename: the file is a bind-mount source for running jails. A rename
    creates a new inode; jails would keep seeing the old one. In-place write
    preserves the inode so jails see the update immediately.

    The write is a single syscall for the full JSON blob (~500 bytes on one
    line), which the kernel delivers atomically on regular files — concurrent
    readers see either the old or the new contents, never a partial mix.
    """
    new_bytes = creds.serialize()
    # Open without O_TRUNC so we control ordering: write first, then truncate
    # any excess length. This avoids a zero-length window in case of a crash
    # between truncate and write (Linux write(2) is atomic against concurrent
    # reads up to PIPE_BUF, but that's ~4K — well above our ~500-byte payload).
    fd = os.open(path, os.O_WRONLY)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        written = os.write(fd, new_bytes)
        if written != len(new_bytes):
            raise OSError(f"short write: {written}/{len(new_bytes)}")
        os.ftruncate(fd, len(new_bytes))
        os.fsync(fd)
    finally:
        os.close(fd)


def refresh_once(
    creds_path: Path,
    lock_path: Path,
    threshold_secs: float,
    force: bool,
    dry_run: bool,
) -> int:
    # --- Load current state ---
    if not creds_path.exists():
        log.error("credentials file not found: %s", creds_path)
        return 2
    try:
        creds = Credentials.load(creds_path)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        log.error("failed to load credentials from %s: %s", creds_path, e)
        return 2

    log.info(
        "loaded credentials: sub=%s tier=%s access=%s refresh=%s %s",
        creds.subscription_type,
        creds.rate_limit_tier,
        fmt_token_prefix(creds.access_token),
        fmt_token_prefix(creds.refresh_token),
        fmt_expiry(creds),
    )

    # --- Decide ---
    if not force and not creds.needs_refresh(threshold_secs):
        log.info(
            "no refresh needed (threshold=%.0fm, headroom=%.1fm)",
            threshold_secs / 60,
            creds.seconds_until_expiry() / 60,
        )
        return 0

    reason = "forced" if force else f"expires within {threshold_secs / 60:.0f}m"
    log.info("refresh needed (%s)", reason)

    if dry_run:
        log.info("[dry-run] would POST to %s", TOKEN_URL)
        log.info("[dry-run] would write new credentials to %s", creds_path)
        return 0

    # --- Lock ---
    try:
        lock_fh = acquire_lock(lock_path, wait_secs=5.0)
    except BlockingIOError:
        log.warning("another refresher is running (lock=%s); skipping", lock_path)
        return 3

    try:
        # Re-read under the lock in case someone else just refreshed while we
        # were waiting.
        creds = Credentials.load(creds_path)
        if not force and not creds.needs_refresh(threshold_secs):
            log.info("refresh no longer needed after re-read under lock")
            return 0

        # --- Refresh ---
        try:
            resp = call_refresh_endpoint(creds.refresh_token)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            log.error("refresh HTTP error: %s %s — %s", e.code, e.reason, body)
            return 1
        except urllib.error.URLError as e:
            log.error("refresh network error: %s", e)
            return 1
        except (json.JSONDecodeError, ValueError) as e:
            log.error("refresh response parse error: %s", e)
            return 1

        try:
            access, refresh, expires_at_ms, scopes = parse_refresh_response(resp)
        except ValueError as e:
            log.error("refresh response missing fields: %s", e)
            return 1

        new_creds = creds.with_new_tokens(access, refresh, expires_at_ms, scopes)

        log.info(
            "refreshed: access=%s→%s refresh=%s→%s expiry=%s",
            fmt_token_prefix(creds.access_token),
            fmt_token_prefix(new_creds.access_token),
            fmt_token_prefix(creds.refresh_token),
            fmt_token_prefix(new_creds.refresh_token),
            fmt_expiry(new_creds),
        )

        # --- Write ---
        write_credentials_in_place(creds_path, new_creds)
        log.info("wrote new credentials to %s", creds_path)
        return 0
    finally:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)
        finally:
            lock_fh.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Refresh the shared Claude Code OAuth token for yolo-jail."
    )
    parser.add_argument(
        "--creds-file",
        type=Path,
        default=DEFAULT_CREDS_PATH,
        help=f"path to .credentials.json (default: {DEFAULT_CREDS_PATH})",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help=f"path to flock file (default: {DEFAULT_LOCK_PATH})",
    )
    parser.add_argument(
        "--threshold-minutes",
        type=float,
        default=DEFAULT_REFRESH_THRESHOLD_SECS / 60,
        help="refresh when access token has less than this many minutes left (default: 30)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="refresh regardless of expiry (burns a refresh token)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen, don't touch the network or disk",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    return refresh_once(
        creds_path=args.creds_file.expanduser(),
        lock_path=args.lock_file.expanduser(),
        threshold_secs=args.threshold_minutes * 60,
        force=args.force,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
