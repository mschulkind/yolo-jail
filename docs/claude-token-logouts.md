# Claude token logouts — diagnosis & fix

Entry point when a jail (or host) prompts `Please run /login · API Error: 401 {"type":"authentication_error"}` more often than once a month.

This doc is user-facing and operational. Background:

- [`HANDOFF-credentials-logout.md`](../HANDOFF-credentials-logout.md) — investigation notes, ruled-out hypotheses, binary offsets.
- [`src/bundled_loopholes/claude-oauth-broker/README.md`](../src/bundled_loopholes/claude-oauth-broker/README.md) — broker architecture + operator ops.
- [`docs/claude-oauth-mitm-proxy-plan.md`](claude-oauth-mitm-proxy-plan.md) — historical design notes for the broker split.

## TL;DR

Repeated logouts almost always mean one of:

1. **Broker loophole isn't installed or primed** — `just deploy` never ran, or CA/leaf state is missing.
2. **Host and jail credentials diverged** — refresh tokens don't match, so the broker's host-file mirror is disabled by design and the host file ages out to expiry.
3. **Broker bypassed** — jail's Claude Code is somehow reaching Anthropic directly (DNS override stripped, CA cert not trusted, loophole disabled) and races another refresher.

All three are diagnosable in under a minute with `yolo doctor` on the host.

## Symptoms

- A jail session returns `API Error: 401 ... Please run /login` mid-task.
- Host `claude` outside a jail prompts for `/login` after you've just logged in recently.
- `stat ~/.local/share/yolo-jail/home/.claude/.credentials.json` shows the file hasn't been touched in hours, even though the token's `expiresAt` has passed.

## Step 1 — run `yolo doctor` on the host

```bash
yolo doctor
```

Scan the Loopholes section for the `claude-oauth-broker` line.

| Symptom | What it means | Fix |
|---|---|---|
| `claude-oauth-broker: inactive — requires.command_on_path 'claude' not met` | `claude` isn't on the host PATH. Broker never activates. | Install Claude Code, or set `loopholes.claude-oauth-broker.enabled: false` if intentional. |
| `NOTE: ca.crt not yet generated` | Fresh install, state dir is empty. | `just deploy` (or `yolo-claude-oauth-broker-host --init-ca` directly). |
| `FAIL: <creds-path>: ...` (JSON parse error) | Shared credentials file exists but is corrupt. | Re-run `claude` and `/login` inside a jail to rewrite. |
| `NOTE: <creds-path> does not exist` | No one has logged in yet. | Start a jail, run `claude`, `/login`. |
| `broker: OK` but jails still 401 | Skip to Step 3. | |

## Step 2 — check for host/jail divergence

Even with the broker running, the **host** `~/.claude/.credentials.json` can fall behind because the broker only mirrors into it when the refresh tokens match.

```bash
python3 - <<'EOF'
import json, datetime, os
for p in ('~/.claude/.credentials.json',
          '~/.local/share/yolo-jail/home/.claude/.credentials.json'):
    p = os.path.expanduser(p)
    try:
        d = json.load(open(p))['claudeAiOauth']
        exp = datetime.datetime.fromtimestamp(d['expiresAt']/1000, tz=datetime.timezone.utc)
        print(f'{p}\n  refreshToken[:16] = {d["refreshToken"][:16]}\n  expiresAt         = {exp.isoformat()}')
    except Exception as e:
        print(p, 'ERR', e)
EOF
```

**Different `refreshToken[:16]` prefixes** mean divergence. Symptoms:

- Host file's `expiresAt` keeps trailing the jail-shared file and eventually goes into the past.
- Each time host `claude` notices, you `/login` again → mints a new host refresh token → divergence persists.

Fix: pick a single source of truth.

- **Using jails only** — leave the host file alone; ignore its expiry. `claude` on the host will fail and you shouldn't run it anyway.
- **Using host `claude` too** — start the broker daemon with `--host-creds-file /dev/null` to disable mirroring entirely, and accept that host and jail sessions drift independently.
- **Re-converge once** — copy the jail-shared file to the host path once (`cp ~/.local/share/yolo-jail/home/.claude/.credentials.json ~/.claude/.credentials.json`), then future refreshes mirror automatically until something knocks them out of sync.

## Step 3 — broker is healthy but jails still 401

At this point the jail is reaching Anthropic directly instead of routing through the broker. Possible causes:

- `NODE_EXTRA_CA_CERTS` not set in the jail (so TLS to the intercepted `platform.claude.com` fails and Claude falls back — though it really shouldn't: confirm with `env | rg CA_CERTS`).
- `--add-host platform.claude.com:127.0.0.1` missing from the podman/docker invocation.
- The in-jail `oauth-broker-jail` daemon crashed — check `cat ~/.local/state/yolo-jail-daemons/claude-oauth-broker.log` inside a jail.

Quick check: watch the shared file's mtime while a jail is running.

```bash
# Terminal 1: inside a running jail
claude auth status

# Terminal 2: on the host
watch -n 1 'stat -c "mtime=%y inode=%i" \
  ~/.local/share/yolo-jail/home/.claude/.credentials.json'
```

If the mtime advances but the host broker daemon's log has nothing around that timestamp (`ls ~/.local/share/yolo-jail/logs/host-service-claude-oauth-broker-*.log`), the jail wrote it directly — broker is being bypassed.

## Manual checks cheat sheet

```bash
# Broker state — CA, leaf, lock
ls ~/.local/share/yolo-jail/state/claude-oauth-broker/

# Broker self-check
yolo-claude-oauth-broker-host --self-check

# Per-jail host-daemon log
ls -lt ~/.local/share/yolo-jail/logs/host-service-claude-oauth-broker-*.log | head

# In-jail TLS terminator log (run inside a jail)
cat ~/.local/state/yolo-jail-daemons/claude-oauth-broker.log

# See the shared file's state
stat ~/.local/share/yolo-jail/home/.claude/.credentials.json
```

## When to update this doc

- A new Claude Code version moves the token endpoint → update `TOKEN_URL` in `src/oauth_broker.py` and note it here.
- A failure mode shows up that doesn't map to Step 1–3 → add it as a new row in Step 1 or a subsection here.
