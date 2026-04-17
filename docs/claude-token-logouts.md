# Claude token logouts — diagnosis & fix

Entry point when a jail (or host) prompts `Please run /login · API Error: 401 {"type":"authentication_error"}` more often than once a month.

This doc is user-facing and operational. Background:

- [`HANDOFF-credentials-logout.md`](../HANDOFF-credentials-logout.md) — investigation notes, ruled-out hypotheses, binary offsets.
- [`scripts/README.md`](../scripts/README.md) — refresher install + troubleshooting by message.
- [`docs/claude-oauth-mitm-proxy-plan.md`](claude-oauth-mitm-proxy-plan.md) — fallback plan if the refresher isn't enough.

## TL;DR

Repeated logouts almost always mean one of:

1. **Refresher not running on the host** — timer never installed, or stale unit after the Apr 15 wheel refactor (`00435a8`).
2. **Host and jail credentials diverged** — refresh tokens don't match, so mirroring is disabled by design and the host file ages out to expiry.
3. **Two writers racing** — host `claude` and the refresher, or the refresher and a jail, refresh in the same window; refresh tokens are single-use, loser gets 401.

All three are diagnosable in under a minute with `yolo doctor` on the host.

## Symptoms

- A jail session returns `API Error: 401 ... Please run /login` mid-task.
- Host `claude` outside a jail prompts for `/login` after you've just logged in recently.
- `stat ~/.local/share/yolo-jail/home/.claude/.credentials.json` shows the file hasn't been touched in hours, even though the token's `expiresAt` has passed.

## Step 1 — run `yolo doctor` on the host

```bash
yolo doctor
```

The refresher checks live in [`src/cli.py:_check_claude_token_refresher`](../src/cli.py). Scan the output for these lines:

| Line | What it means | Fix |
|---|---|---|
| `FAIL: claude-token-refresher binary not on PATH` | Wheel not installed, or PATH missing `~/.local/bin`. | `uv tool install --force <wheel>` or `pip install -e .` from the repo; verify with `which claude-token-refresher`. |
| `WARN: Refresher systemd units not installed` | `just deploy` never ran (or ran before the systemd template was correct). | `cd ~/code/yolo-jail && just deploy`. |
| `WARN: Service unit ExecStart does not point at the installed binary` | Stale unit file from before the Apr 15 refactor (old path: `scripts/claude-token-refresher.py`, new: wheel entry point). | `just deploy` re-templates and re-installs. |
| `WARN: Refresher timer not enabled` / `not active` | Units installed but never started. | `systemctl --user enable --now claude-token-refresher.timer`. |
| `FAIL: Refresher service last run failed` | Timer fires but the refresh itself errors. | `journalctl --user -u claude-token-refresher -n 50 --no-pager` — usually a 401 (burned refresh token) or a 404 (endpoint moved). See [`scripts/README.md`](../scripts/README.md#troubleshooting). |
| `FAIL: Access token expired Nm ago` | Refresher is not running or not writing. | Combine with the lines above; start with binary/unit/timer checks. |

If every line is green but logouts continue → skip to Step 3.

## Step 2 — check for host/jail divergence

Even with the refresher running, the **host** `~/.claude/.credentials.json` can fall behind because the refresher only mirrors to it when the refresh tokens match.

```bash
python3 - <<'EOF'
import json, datetime
for p in ('~/.claude/.credentials.json',
          '~/.local/share/yolo-jail/home/.claude/.credentials.json'):
    import os; p = os.path.expanduser(p)
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
- **Using host `claude` too** — run the refresher with `--host-creds-file /dev/null` to disable mirroring entirely, and accept that host and jail sessions drift independently.
- **Re-converge once** — copy the jail-shared file to the host path once (`cp ~/.local/share/yolo-jail/home/.claude/.credentials.json ~/.claude/.credentials.json`), then future refreshes mirror automatically until something knocks them out of sync.

## Step 3 — refresher is healthy but jails still 401

At this point the failure mode is most likely that Claude Code inside jails is refreshing on its own despite the shared file being fresh — i.e. the `ak4()` mtime-reread assumption described in [`HANDOFF-credentials-logout.md`](../HANDOFF-credentials-logout.md#unexplained--what-to-investigate-next) doesn't hold.

Quick check: watch refresher activity while a jail is running.

```bash
# Terminal 1: inside a running jail
claude auth status

# Terminal 2: on the host
watch -n 1 'stat -c "mtime=%y inode=%i" \
  ~/.local/share/yolo-jail/home/.claude/.credentials.json'
```

If the mtime advances with no corresponding refresher journal entry, a jail wrote it → jails are still refreshing.

When that happens, the next step is the MITM broker in [`docs/claude-oauth-mitm-proxy-plan.md`](claude-oauth-mitm-proxy-plan.md). The broker terminates TLS for `platform.claude.com` and serializes *all* refreshes — jails never talk to Anthropic directly, so they can't race.

## Manual checks cheat sheet

```bash
# Refresher: installed, unit, timer
which claude-token-refresher
systemctl --user status claude-token-refresher.timer
systemctl --user status claude-token-refresher.service
journalctl --user -u claude-token-refresher -n 50 --no-pager

# One-shot dry run — no network, no writes
claude-token-refresher --dry-run -v

# Force a refresh right now (burns a refresh token)
claude-token-refresher --force

# See the shared file's state
stat ~/.local/share/yolo-jail/home/.claude/.credentials.json
```

## When to update this doc

- A new Claude Code version moves the token endpoint or changes the write path → update [`scripts/README.md`](../scripts/README.md) and add a note here.
- The MITM broker ships → add a "Step 4: broker is running but …" section.
- A failure mode shows up that doesn't map to Step 1–3 → add it as a new table row in Step 1 or a subsection here.
