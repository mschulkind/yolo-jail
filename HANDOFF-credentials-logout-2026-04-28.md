# Handoff — Claude credential logout investigation (2026-04-28)

Successor to [`HANDOFF-credentials-logout.md`](HANDOFF-credentials-logout.md) (2026-04-12). The earlier handoff is still relevant for binary offsets and ruled-out hypotheses; this one captures findings from the broker era (post-singleton refactor) and reframes the problem.

> **Stop the bandaid loop.** The user has been through ~17 iterations of "this commit fixes it; restart and try again." Every iteration has fixed *a* thing and the next failure has a slightly different shape. **Do not commit a fix without a falsifiable root cause + a way to verify it doesn't recur.** Restart-the-jail is the symptom, not the diagnosis.

## Status as of 2026-04-28T19:10Z

The user got logged out *again* in a jail. They restarted a jail (without doing `/login`) and it came back logged in. Then asked for a proper investigation rather than another bandaid.

**Forensic snapshot (host paths)** — captured at the time of the report and archived in this handoff for posterity:

| Path | Refresh token prefix | expiresAt (UTC) | mtime (local) |
|---|---|---|---|
| `~/.claude/.credentials.json` | `sk-ant-ort01-u6j…` | 2026-04-28 10:13:09 | 22:13:09 -0400 |
| `~/.local/share/yolo-jail/home/.claude-shared-credentials/.credentials.json` (initial) | `sk-ant-ort01-bCL…` | 2026-04-28 02:06:37 | 14:06:37 -0400 (Apr 27) |
| `~/.local/share/yolo-jail/home/.claude-shared-credentials/.credentials.json` (after restart) | `sk-ant-ort01-ses…` | 2026-04-28 22:56:22 | 14:56:22 -0400 (Apr 28) |

Three different refresh tokens — host, jail-stale, jail-fresh — confirms the post-`cb6e850` design: **host and jail are independent identities**.

## Architecture as it actually is (broker era)

Authoritative now; supersedes the old handoff's "shared bind-mounted file" picture.

```
[Claude Code in jail]
   |  POST https://platform.claude.com/v1/oauth/token
   |  /etc/hosts pin: platform.claude.com → 127.0.0.1
   v
[oauth_broker_jail.py — in-jail TLS terminator on :443]
   |  CA-signed leaf cert, NODE_EXTRA_CA_CERTS trusts the broker CA
   |  forwards request body via Unix socket
   v
[/run/yolo-services/claude-oauth-broker.sock] (inside jail)
   |  bind-mounted from /tmp/yolo-claude-oauth-broker.sock (host)
   v
[yolo-claude-oauth-broker-host — singleton on host]
   |  pid file /tmp/yolo-claude-oauth-broker.pid
   |  flock around shared creds at
   |  ~/.local/share/yolo-jail/home/.claude-shared-credentials/.credentials.json
   |  on cache miss: POST upstream platform.claude.com/v1/oauth/token
   v
[Anthropic OAuth issuer]
```

Per-workspace `.yolo/home/claude/.credentials.json` is a relative symlink (`../.claude-shared-credentials/.credentials.json`). It only resolves correctly *inside* the jail, where `.claude-shared-credentials/` is mounted next to `.claude/`. On the host, `readlink -f` reports BROKEN — that is **expected and not a bug** (don't waste time on it like the old handoff did).

`/tmp/yolo-host-services-<jail>/claude-oauth-broker.sock` on the host is a 0-byte regular file. That is a **bind-mount source placeholder**, not a forwarder socket. Inside the jail, the bind mount makes `/run/yolo-services/claude-oauth-broker.sock` actually be the singleton's socket. **Do not be misled by the placeholder being a regular file** — it is supposed to be.

## Confirmed live (today, from inside `yolo-yolo-jail-887995ca`)

```
$ ls -l /run/yolo-services/claude-oauth-broker.sock
srw------- root root 0 …    <-- real socket via bind mount
$ python3 -c 'send {"action":"ping"}'
{"pong": true, "pid": 14479}        <-- singleton replies
```

So the OAuth path is wired up correctly. The 401 in the jail is *not* due to a network/socket plumbing failure today.

## The big new finding — refresh-grants are missing from the wire

Across the entire singleton broker log (`~/.local/share/yolo-jail/logs/host-service-claude-oauth-broker.log`) and the in-jail TLS terminator log (`/home/agent/.local/state/yolo-jail-daemons/claude-oauth-broker.log`), **every** `POST /v1/oauth/token` from a jail is `body_len=325 is_refresh=False`.

`is_refresh=False` plus consistent body length 325 means PKCE `grant_type=authorization_code` (i.e. `/login`). Refresh-token grants would be `is_refresh=True` and substantially smaller (~150 bytes — just `grant_type=refresh_token` plus the token). **Not a single one has been observed in the broker's history.**

Cadence of jail-side `/v1/oauth/token` calls (singleton log, all status=200 except where noted):

- 2026-04-23 20:32, 20:56 (2x)
- 2026-04-24 06:24, 06:29, 07:07 (3x within an hour)
- 2026-04-25 21:42, 21:43, 21:43 (3x within 1 minute)
- 2026-04-26 15:08 (terminator restart), 15:09 (4x), 15:31, 15:32 (2x)
- 2026-04-27 14:06 → **400 status, body_len=93** (this is the failure that started today's logout)

That's the loop: Claude in jail can't refresh → access token expires → next request returns 401 → Claude falls through to `/login` → user re-authenticates → repeat.

### Why does Claude never send a refresh-grant?

**ANSWERED 2026-04-28 (in-jail strace experiment, after this handoff was first written):** Claude Code does not refresh on a 401. When the access token is rejected by the server, Claude jumps straight to `/login` — no `grant_type=refresh_token` POST is ever emitted. Verified by:

1. Restoring `expiresAt` to a real future value, corrupting only the `accessToken` field in shared creds.
2. Running `claude -p 'reply OK'` under `strace -f -e trace=network`.
3. Observing: server returns 401 "Invalid bearer token", Claude prints "Please run /login", and the strace contains no `connect()` to 127.0.0.1:443 (the platform.claude.com intercept) and no `grant_type` write.

So the bug is in Claude Code's client logic. Claude only attempts refresh when it *itself* decides the token is near expiry (using the `expiresAt` from `.credentials.json` as a hint). It does not attempt refresh in response to a server-returned 401. Once Anthropic invalidates a token (e.g. wall-clock past expiresAt, or revoked, or any server-side reason), the local-file `expiresAt` is stale — and Claude trusts the stale local value, skips refresh, sends an invalid token, gets 401, falls through to `/login`.

That means **the broker can only help by refreshing tokens *proactively before they expire*, not reactively when a 401 happens.** On-demand refresh as currently implemented (broker only refreshes when a jail explicitly POSTs `grant_type=refresh_token` to `/v1/oauth/token`) is dead code in practice — Claude never makes that POST.

This invalidates the architectural premise of the post-`02821aa` design ("remove refresher, fold on-demand refresh into the broker"). The refresher needs to come back, or its function needs to be folded into something that runs on a wall-clock schedule.

### How did today's restart "fix" it without /login?

The shared creds got rewritten at **14:56:22 -0400** on Apr 28 with a *new* refresh token (`ses…`) that didn't come from any visible `/v1/oauth/token` proxy event. The broker singleton daemon (pid 14479) **logged nothing for 17.5 hours** between `2026-04-27 21:28:43` and `2026-04-28 15:05:07`. The shared file got rewritten *during that gap*.

So either:
- The broker DID process a request during the gap but didn't log it (silent code path? log handler wedged?). Possible: there is also an `action=refresh` direct path (`oauth_broker.py:do_refresh`) callable by the broker subcommand or a self-prime pathway that may not log identically to proxy.
- Or something other than the broker wrote those new tokens — the entrypoint's `_sync_host_claude_files` path? Some other refresher we don't know about?

This is the **second open architectural question**: what wrote the shared creds at 14:56:22? The new refresh token is *different from host's*, so it's not a host→shared sync. It's a freshly-minted token from Anthropic, but not via a path that landed in the broker log. Trace it.

## What the existing playbook gets wrong

`docs/claude-token-logouts.md` and `yolo doctor` mislead in ways the next agent should not chase:

1. **`yolo doctor` "claude-oauth-broker @ <jail>: socket dead"** is a **false positive**. The probe (cli.py:5336) connects directly to the host-side path `/tmp/yolo-host-services-<jail>/claude-oauth-broker.sock`, which is the bind-mount source placeholder (regular file). It is *not* a socket. The probe should either (a) `podman exec` into the jail and connect to `/run/yolo-services/...` from there, (b) probe the singleton at `/tmp/yolo-claude-oauth-broker.sock` directly, or (c) be deleted. Until then, ignore "socket dead" warnings entirely. **Fix this before debugging anything else** — it sends investigators down a dead path.

2. **The doctor "host/jail divergence" advice still references the old `_sync_host_claude_files` mirror logic, which post-`cb6e850` no longer mirrors.** Host and jail are *intentionally* separate identities; "divergence" is the design, not a symptom. The triage doc needs an update.

3. **Doctor reports the singleton broker daemon as "live (pid=…, ping ok)" while the jail is silently broken for 17 hours.** Liveness ≠ functional. A useful health check would be: time since last successful refresh-grant, vs. time since shared creds last advanced.

## Investigation playbook (to run from inside the jail)

The next agent should pick up here. All paths below are the **in-jail** view (`/workspace` is this repo).

### Step 1 — instrument and reproduce

Capture a real refresh attempt under strace before it gets eaten silently:

```bash
# Inside the jail, in a workspace clone of this repo:
cd /workspace

# 1. Force a near-expiry token. Edit the shared file's expiresAt
#    to ~60s in the future. Don't change the refresh token.
python3 -c '
import json, os, time, pathlib
p = pathlib.Path("/home/agent/.claude-shared-credentials/.credentials.json")
d = json.loads(p.read_text())
d["claudeAiOauth"]["expiresAt"] = int((time.time() + 60) * 1000)
p.write_text(json.dumps(d))
print("primed expiresAt to T+60s")
'

# 2. Tail the broker log on the host side (separate terminal):
#    tail -F ~/.local/share/yolo-jail/logs/host-service-claude-oauth-broker.log
#    tail -F ~/.local/state/yolo-jail-daemons/claude-oauth-broker.log

# 3. Strace Claude making one cheap request:
strace -f -y -s 200 -e trace=connect,sendto,write,openat,rename \
   -o /tmp/claude.strace \
   claude -p 'echo test' 2>&1 | head

# 4. Look for grant_type in the strace output:
grep -E 'grant_type|refresh_token|oauth/token' /tmp/claude.strace
```

The decisive question: does Claude's strace show a `grant_type=refresh_token` POST being emitted at all? If yes — the request is going somewhere other than the TLS terminator. If no — Claude's internal refresh path is short-circuited (hypothesis #1 above), and the next step is reverse-engineering `Hf()`/`Eg()` in the binary.

### Step 2 — answer "what wrote shared creds at 14:56 today"

```bash
# On the host: enable inotify watch on the shared dir before the next restart,
# then restart a logged-out jail and capture which process touches the file.
inotifywait -m -e modify,close_write,moved_to,create \
  ~/.local/share/yolo-jail/home/.claude-shared-credentials/ &
yolo restart <some-jail>     # if such a subcommand exists, otherwise:
# podman restart yolo-<workspace>-<hash>
```

Cross-reference the inotify timestamps with broker log entries. If broker log is silent but shared creds change, the writer is NOT the broker — and that's important.

### Step 3 — fix the doctor probes (low-hanging)

`src/cli.py:5336` per-jail liveness probe is checking the wrong thing. Either remove it or make it `podman exec <jail> python3 -c '<ping snippet>'`. This is a small, isolated fix and removes a permanent source of misdirection.

### Step 4 — health metric the actual symptom

The user's symptom is "I get logged out N times a day." A meaningful health check, surfacable by `yolo doctor`:

- "Time since last successful refresh-grant on the wire" (not /login). Goal: regular refreshes every ~7-8h on a logged-in jail.
- "Time since shared creds expiresAt was advanced." If creds haven't been rewritten before they expire, that's the bug, regardless of why.
- Alarm at, say, `expiresAt < now + 2h` AND `mtime > expiresAt - 8h` ⇒ "creds will expire and broker hasn't refreshed."

This makes regression visible. Even if step 1's root-cause investigation takes weeks, this metric prevents users (matt) from being surprised by a 401 during real work.

## Open architectural questions (the *real* ones)

1. **Does Claude Code attempt refresh-token grants at all?** Falsifiable via Step 1 strace. Until answered, every "broker fix" is theater — the broker can't refresh tokens that the client never asks it to.

2. **What wrote shared creds during the broker's 17.5-hour log gap on 2026-04-27 → 2026-04-28?** Falsifiable via Step 2 inotify. Until answered, we don't know which process is the actual refresh path.

3. **Is the broker's flock helping or hurting?** The flock is held during refresh. If a refresh hangs (e.g. upstream slow), all jails block. If 6 jails are running and one is mid-refresh, the others 401 on a serialized wait. The old handoff lead #3 (lockfile location) is still open in the singleton model.

4. **Should the per-jail TLS terminator exist at all?** Each jail boots its own Python TLS terminator that ONLY exists to forward `/v1/oauth/token` POSTs to the singleton. That's a per-jail process that can crash, hang, or run stale code (the binary was edited 2026-04-26 14:05 to current). Alternative architectures:
   - Network namespace: jail talks `platform.claude.com` directly via host network, host iptables redirects. No per-jail Python.
   - Skip the proxy entirely: stop intercepting `platform.claude.com`, let Claude in the jail talk directly to it. Use the broker only for the *shared file* coordination (one writer, multiple readers) — refreshing happens client-side per-jail with serialization via flock around the file write.

The second option is genuinely radical (it removes the whole MITM apparatus) but has the benefit of *not having an architectural component that can fail in the way today's failed*. Worth designing on paper before another bandaid round.

## Recurring bandaid pattern to break

Recent commits since 2026-04-12 that landed under "fix(broker)":

- `9a27ebe` set User-Agent so Cloudflare doesn't 1010-ban refreshes
- `46d5417` doctor self-checks
- `021a009` broker self-check diagnoses systemd state + port-443 failures
- `46c6981` docker_args — never overlay a file mount on a dir mount
- `03cb3d8` replace single-file credentials mount with directory mount + symlink
- `c270dfe` update broker DEFAULT_CREDS_PATH to new shared-credentials dir
- `02821aa` remove refresher, fold on-demand refresh into the broker
- `cb6e850` stop mirroring creds to host; separate identities
- `64323e1` mirror /login response into shared creds; one login wakes all jails
- `e7b7073` singleton host daemon + yolo broker subcommand + doctor liveness
- `86a53c4` route /login traffic through the host broker
- `a59cd66` _broker_kill falls back to pgrep when PID file missing
- `45bf66f` add token-fingerprint + file-snapshot debug logging
- `97eb414` doctor: include 'spawned' lifecycle in liveness probe

Each one fixed something real. None addressed: **why does Claude in the jail never send refresh-grants?** That question hasn't been formally posed in any commit message. Pose it. Answer it before the next "fix(broker): …".

## Pointers

- Broker host source: `src/oauth_broker.py` (do_refresh:388, do_proxy:479, _refresh_upstream:325, /login mirror logic ~583)
- Jail TLS terminator: `src/oauth_broker_jail.py` (UPSTREAM_HOST:53, _is_refresh_grant:186)
- CLI bind-mount setup: `src/cli.py:7140-7180` (the dir mount + file mount overlay for the singleton socket)
- Singleton helpers: `src/cli.py:1421-1721` (_broker_ping, _broker_ensure, _broker_kill, BROKER_SINGLETON_*)
- Doctor probe (the misleading one): `src/cli.py:5292-5336`
- Old handoff (binary offsets, refresh path bytes): `HANDOFF-credentials-logout.md`
- User-facing triage doc (needs update): `docs/claude-token-logouts.md`
- Broker README: `src/bundled_loopholes/claude-oauth-broker/README.md`

## Logs preserved at write time

Should be unchanged when the next agent picks this up, but capture in case of rotation:

- `~/.local/share/yolo-jail/logs/host-service-claude-oauth-broker.log` (singleton broker, 474 lines as of 2026-04-28T19:08Z)
- `/home/agent/.local/state/yolo-jail-daemons/claude-oauth-broker.log` per jail (in `yolo-yolo-jail-887995ca`: 3.1 MB, last entry 2026-04-27 14:06:20)
- `~/.local/share/yolo-jail/state/claude-oauth-broker/` for CA + lock state
