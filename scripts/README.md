# yolo-jail scripts

Host-side scripts that support the jail. These run on the host, not inside any jail.

## `claude-token-refresher.py`

Periodically refreshes the shared Claude Code OAuth token so jails never have to. Solves the "repeatedly logged out" problem caused by refresh-token rotation racing across jails.

**Context:** `docs/claude-oauth-mitm-proxy-plan.md` and `HANDOFF-credentials-logout.md` explain the full root cause. Short version: Anthropic's OAuth server uses single-use refresh tokens; when two jails refresh at the same time, the loser gets 401 and forces the user to re-login. This script runs on the host as a single serialized refresher, keeps the shared `.credentials.json` fresh, and Claude Code inside jails reloads it via its own mtime-cache path without needing to refresh on its own.

### Quick start

```bash
# Dry-run — check state, print what would happen, no network, no writes:
./scripts/claude-token-refresher.py --dry-run -v

# Real run — refresh the token if it expires within 30 minutes (default threshold):
./scripts/claude-token-refresher.py

# Force a refresh right now (burns a refresh token — use sparingly):
./scripts/claude-token-refresher.py --force
```

Default credentials file: `~/.local/share/yolo-jail/home/.claude/.credentials.json` — the shared file that jails bind-mount. Override with `--creds-file`.

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | Success (refreshed, no refresh needed, or dry-run) |
| 1 | Transient failure (network, non-2xx from upstream) |
| 2 | Permanent failure (file missing, corrupt, no refresh token) |
| 3 | Lock contention (another refresher is running) |

Never logs full token values, only 12-character prefixes.

### Install via `just deploy` (Linux — recommended)

On Linux, `just deploy` handles this automatically: it templates `claude-token-refresher.service` with the real repo path, writes it and the `.timer` to `~/.config/systemd/user/`, reloads systemd, enables + starts the timer, and fires one service run immediately. Idempotent, safe to re-run.

```bash
cd ~/code/yolo-jail
just deploy

# Verify:
systemctl --user list-timers claude-token-refresher
systemctl --user status claude-token-refresher
journalctl --user -u claude-token-refresher -n 50
```

The timer fires every 10 minutes. Steady-state behavior: the refresher wakes up, sees the token has plenty of headroom, exits in ~50 ms. Once every ~7.5 hours it actually hits the network and writes a new token to the shared file.

If your systemd user instance doesn't run when you're not logged in, enable lingering:

```bash
sudo loginctl enable-linger "$USER"
```

### Install on macOS (launchd — recommended)

macOS has no `systemd --user`, so `just deploy` detects this and skips the systemd install. Use launchd instead — `launchctl` runs user agents without a GUI session, survives logout, and is the native approach.

Create `~/Library/LaunchAgents/com.yolojail.claude-token-refresher.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yolojail.claude-token-refresher</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/code/yolo-jail/scripts/claude-token-refresher.py</string>
  </array>

  <key>StartInterval</key>
  <integer>600</integer>  <!-- every 10 minutes -->

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/tmp/claude-token-refresher.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/claude-token-refresher.log</string>
</dict>
</plist>
```

Replace `/Users/YOU/code/yolo-jail/` with your actual repo path, then:

```bash
launchctl load ~/Library/LaunchAgents/com.yolojail.claude-token-refresher.plist

# Verify it's running and scheduled:
launchctl list | grep yolojail
tail -f /tmp/claude-token-refresher.log
```

To stop/remove:

```bash
launchctl unload ~/Library/LaunchAgents/com.yolojail.claude-token-refresher.plist
```

### Install as a cron job (fallback — Linux or macOS)

Works everywhere but has no structured logging and doesn't survive reboots cleanly on macOS.

```bash
# Every 10 minutes:
( crontab -l 2>/dev/null; echo '*/10 * * * * /path/to/yolo-jail/scripts/claude-token-refresher.py >> /tmp/claude-refresher.log 2>&1' ) | crontab -
```

### Troubleshooting

First line of defence: `yolo doctor` on the host. It runs `_check_claude_token_refresher` which walks binary presence → credentials file → systemd unit → timer state → last-run status and points at the fix for each. If you're triaging a logout, start there, then match the message below.

For a full decision tree (symptoms → doctor → fix → escalate to MITM broker), see [`docs/claude-token-logouts.md`](../docs/claude-token-logouts.md).

**"claude-token-refresher binary not on PATH" (doctor)** — the wheel isn't installed, or `~/.local/bin` isn't on PATH. Install with `uv tool install --force <wheel>` (or `pip install -e .` from the repo), then verify `which claude-token-refresher` resolves. Before the Apr 15 refactor (`00435a8`) the refresher was a loose script at `scripts/claude-token-refresher.py`; afterwards it's a console entry point inside the wheel.

**"Service unit ExecStart does not point at the installed binary" (doctor)** — you have a stale systemd unit from before the wheel refactor. The old unit hardcoded `scripts/claude-token-refresher.py`; that path no longer exists. Re-run `just deploy` from the repo to re-template and reinstall.

**"Refresher service last run failed" / red `● claude-token-refresher.service`** — timer is firing but the refresh itself errors. Read the last 50 lines: `journalctl --user -u claude-token-refresher -n 50 --no-pager`. Most common causes are the two HTTP errors below.

**"refresh HTTP error: 401"** — the refresh token the script started with has already been invalidated, probably because a jail raced and won before you deployed the refresher (or you `/login`'d on the host after the refresher last ran and the tokens diverged). Run `/login` in any Claude Code session to mint a new credential, then restart the timer.

**"refresh HTTP error: 404"** — the token endpoint moved. This script has `platform.claude.com/v1/oauth/token` hardcoded (pulled from the 2.1.101 binary). Re-verify with:

```bash
rg -oab 'platform\.claude\.com|/v1/oauth/token' ~/.local/share/claude/versions/*/claude | head
```

And update `TOKEN_URL` at the top of the script.

**"another refresher is running"** — two timer instances fired concurrently (rare — systemd should serialize). Benign, next tick will retry.

**Host `~/.claude/.credentials.json` keeps expiring** — the jail-shared file is fresh but the host file lags and forces you to `/login` on the host. This is the diverged-refresh-token case: mirroring is disabled (see "Host Claude Code interaction" below). Re-converge once with `cp ~/.local/share/yolo-jail/home/.claude/.credentials.json ~/.claude/.credentials.json`, or disable mirroring entirely with `--host-creds-file /dev/null` if you want host and jail sessions to live independently.

**Jails still getting logged out after deploying this** — the refresher is keeping the file fresh, but Claude Code inside jails may not be reloading it on its mtime check path (the `ak4()` assumption in `HANDOFF-credentials-logout.md`). Confirm by watching `stat` on the shared file during a jail session — if mtime advances without a corresponding refresher journal entry, jails are still refreshing. The next step in that case is the MITM proxy — see `docs/claude-oauth-mitm-proxy-plan.md`.

### Host Claude Code interaction

Host Claude Code (running directly on your Mac/Linux host, not inside a jail) uses `~/.claude/.credentials.json`. On first jail boot the jail seeds its own credentials file from this host file, so both files initially share the same refresh token. Left unhandled, the refresher would then refresh the jail-shared file and invalidate the shared refresh token, locking the host out.

**The refresher automatically mirrors the refresh to `~/.claude/.credentials.json` when — and only when — that file has the same refresh token as the jail-shared file.** If the two files have different refresh tokens (you have separate host/jail logins on purpose), the host file is left strictly alone.

Control:

- Default host-file path: `~/.claude/.credentials.json`. Override with `--host-creds-file PATH`.
- Disable mirroring: `--host-creds-file /dev/null`.

Any already-running host `claude` process caches the old tokens in memory. After the refresher mirrors the file, restart the host Claude process (or let it hit a 401 and re-read the file) to pick up the new tokens.

### What this doesn't fix

- **Initial `/login`** — you still need to authenticate once. The refresher only extends the session, it doesn't create it.
- **Concurrent host Claude Code refresh** — if host Claude Code tries to refresh at the same tick as the refresher, one will invalidate the other's refresh token. The in-process `flock` serializes concurrent refreshers but not a concurrent Claude Code process. In practice this is rare because host Claude only refreshes on token expiry and the refresher's aggressive cadence keeps both files ahead of that window.
- **Long-term refresh token expiry** — Anthropic may eventually invalidate refresh tokens after some absolute lifetime (~1 year is common). When that happens, you'll need to `/login` again. The refresher can't rescue you from that.
