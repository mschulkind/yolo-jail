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

### Install as a systemd user timer (recommended)

```bash
# Assumes the repo is at ~/code/yolo-jail.  Edit the ExecStart path in the
# .service file if you keep it elsewhere.

mkdir -p ~/.config/systemd/user
cp scripts/claude-token-refresher.service ~/.config/systemd/user/
cp scripts/claude-token-refresher.timer   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now claude-token-refresher.timer

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

### Install as a cron job (alternative)

```bash
# Every 10 minutes:
( crontab -l 2>/dev/null; echo '*/10 * * * * /home/YOU/code/yolo-jail/scripts/claude-token-refresher.py >> /tmp/claude-refresher.log 2>&1' ) | crontab -
```

### Troubleshooting

**"refresh HTTP error: 401"** — the refresh token the script started with has already been invalidated, probably because a jail raced and won before you deployed the refresher. Run `/login` in any Claude Code session to mint a new credential, then restart the timer.

**"refresh HTTP error: 404"** — the token endpoint moved. This script has `platform.claude.com/v1/oauth/token` hardcoded (pulled from the 2.1.101 binary). Re-verify with:

```bash
rg -oab 'platform\.claude\.com|/v1/oauth/token' ~/.local/share/claude/versions/*/claude | head
```

And update `TOKEN_URL` at the top of the script.

**"another refresher is running"** — two timer instances fired concurrently (rare — systemd should serialize). Benign, next tick will retry.

**Jails still getting logged out after deploying this** — the refresher is keeping the file fresh, but Claude Code inside jails may not be reloading it on its mtime check path (the `ak4()` assumption in `HANDOFF-credentials-logout.md`). In that case, the next step is the MITM proxy — see `docs/claude-oauth-mitm-proxy-plan.md`.

### What this doesn't fix

- **Initial `/login`** — you still need to authenticate once. The refresher only extends the session, it doesn't create it.
- **Races with *host-side* Claude Code** — if you run Claude Code on the host (not in a jail) against the same shared credentials file, and it tries to refresh at the same time as the host timer, there's still a small race. The `flock` serializes concurrent refreshers but not a concurrent Claude Code process. Mitigate by pointing host-side Claude Code at `~/.claude/` instead of the shared file.
- **Long-term refresh token expiry** — Anthropic may eventually invalidate refresh tokens after some absolute lifetime (~1 year is common). When that happens, you'll need to `/login` again. The refresher can't rescue you from that.
