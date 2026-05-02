# Handoff — Claude credential logouts: root cause found (2026-04-28)

Successor to [`HANDOFF-credentials-logout-2026-04-28.md`](HANDOFF-credentials-logout-2026-04-28.md). That doc framed the investigation; this one **identifies the bug** and proposes the fix.

> **TL;DR:** `cb6e850` removed *half* of the host↔jail credential sharing.  The other half — `_sync_host_claude_files` in `src/entrypoint.py` — still copies `~/.claude/.credentials.json` into the jail's shared creds dir at every jail start.  This re-introduces the exact rotation race `cb6e850`'s commit message says it was eliminating.  The simultaneous host+jail logout the user hit on 2026-04-28T21:00Z is the predicted outcome.

## The bug

`src/entrypoint.py:1119` — `_sync_host_claude_files()` runs at every jail start.  Driven by env var `YOLO_HOST_CLAUDE_FILES`, set by `src/cli.py:7547` from `DEFAULT_HOST_CLAUDE_FILES = ["settings.json", ".credentials.json"]` at `src/cli.py:3590`.

For `.credentials.json`, the function:
1. Reads host's `~/.claude/.credentials.json` (mounted at `/ctx/host-claude/.credentials.json`).
2. Reads the destination at `~/.claude-shared-credentials/.credentials.json` (the jail's shared creds).
3. If destination's `expiresAt >= source's expiresAt`, skips. Otherwise, overwrites destination with source.

So every jail start where the host has a fresher token replaces shared creds with host's tokens. Host and jail then share a refresh token. Whichever rotates first invalidates the other → 401 → `/login` prompt.

This is exactly the failure mode `cb6e850` ("stop mirroring creds to host; separate identities") was supposed to eliminate. But `cb6e850` only removed the **shared→host** mirror in `oauth_broker.py`. The **host→shared** sync in `entrypoint.py` predates that commit (`4ee9701`, 2026-03-30) and is still alive.

## Why this matches every observation

| Observation | Explanation |
|---|---|
| Three different refresh tokens snapshot 2026-04-28T19:10Z (host / jail-stale / jail-fresh) | jail-stale is from before a refresh; jail-fresh was rotated by some client; host is fresh after a separate `/login`. Identities aren't independent — they're racing. |
| Mystery shared-creds rewrite at 2026-04-28T14:56:22 | A jail was started/restarted near that time; entrypoint's `_sync_host_claude_files` copied host's creds in.  Not the broker, not a manual copy — the entrypoint script. |
| Host AND jail simultaneously logged out 2026-04-28T~21:00Z | They were sharing one refresh token (via the entrypoint copy).  Either client rotated it, leaving the other holding an invalidated token.  Both 401 on next request. |
| Singleton broker log shows ZERO `is_refresh=True` POSTs across all of 2026-04-23→28 | Doesn't tell us whether Claude refreshes — this only tells us no refresh from a jail goes through the broker proxy.  It's possible Claude in the jail does refresh and the request lands fine via the in-jail TLS terminator → host singleton → upstream — and we'd see it logged.  But a more likely explanation: the rotation race kills the refresh token before Claude in the jail decides to refresh.  Host refreshes, host rotates, jail's copy is stale; jail uses access token (still valid) until it 401s, then jumps to `/login`. |
| Claude on the host "just works" most of the time | Host is the dominant client of the shared token most days; it refreshes when needed, rotation lands there.  Jail breaks because the rotation is upstream of the jail's stale copy. |

## The fix (proposed, not committed)

**One-line change:** remove `.credentials.json` from `DEFAULT_HOST_CLAUDE_FILES` in `src/cli.py:3590`.

```python
# src/cli.py
-DEFAULT_HOST_CLAUDE_FILES = ["settings.json", ".credentials.json"]
+DEFAULT_HOST_CLAUDE_FILES = ["settings.json"]
```

Optional cleanups (separate commit):
- Delete the `.credentials.json` branch in `_sync_host_claude_files` (`src/entrypoint.py:1138-1149`).
- Delete `_credentials_expiry` helper at `src/entrypoint.py:1107` (becomes unused).
- Delete the `if fname == ".credentials.json"` dst-routing logic (becomes unused).

## Migration impact

`cb6e850`'s commit message already called this out: *"Users who had a shared identity will need one extra `/login` on host after this lands."*  That migration didn't actually happen because the entrypoint sync kept silently re-converging the identities.  Now it will:

- **Existing users with valid shared creds:** unaffected.  Broker's `/login` mirror keeps shared creds fresh as long as the user does at least one `/login` from inside a jail.  Per the handoff, refreshes are not currently observed in the jail-side proxy logs — so the question of "what keeps shared creds fresh long-term?" needs a second answer (the proactive refresher follow-up, see below).
- **Fresh installs / users who only `/login`-ed on the host:** would need to `/login` once from inside a jail to populate shared creds.  This is the migration `cb6e850` intended.

## Resolved: does Claude in the jail refresh at all?

**Answered 2026-04-28T19:24Z by host-side experiment.** Host Claude DOES refresh proactively. Procedure:

1. Snapshot `~/.claude/.credentials.json`.
2. Edit local `expiresAt` to `(now - 60s) * 1000`. Leave the access/refresh tokens intact.
3. Run `claude -p 'reply OK'` — exits with code 0 in under 1s.
4. Observe: `expiresAt` advanced to T+8h, `accessToken` and `refreshToken` are both NEW. The file's sha256 changed.

So Claude refreshes when its local `expiresAt` is in the past. Hypothesis 2 is refuted. Hypothesis 1 (TLS terminator/log instrumentation gap) is also probably wrong — the broker log records `is_refresh=False` based on the parsed body's `grant_type` field, not on body length alone. A real refresh-grant would show as `is_refresh=True`.

The most likely explanation for "zero refresh-grants in the broker log" is now: **the entrypoint sync kept rewriting the shared file with a future expiresAt at every jail boot, so Claude in the jail rarely or never saw a stale local expiresAt — and so never tried to refresh.** That fits the data: jail-side `/v1/oauth/token` POSTs *do* appear in the log, but they're all `authorization_code` (i.e. `/login`), which Claude resorts to *after* getting a 401 from a stale-but-not-yet-locally-expired access token. (The other handoff documented this 401-fallthrough path: Claude does not retry-with-refresh on a 401, only on a local-expiry check.)

**Implication: the proactive refresher (removed in `02821aa`) is NOT needed.** Claude's own client logic does the refresh — provided we stop overwriting its local `expiresAt`. Land the entrypoint fix, leave Claude alone, and refresh-grants should start appearing in the broker log naturally.

## Host-side experiment — RUN, result above (kept here for reproducibility)

```bash
cp ~/.claude/.credentials.json /tmp/creds-snapshot-$(date +%s).json

python3 - <<'PY'
import json, time, pathlib
p = pathlib.Path.home() / ".claude/.credentials.json"
d = json.loads(p.read_text())
d["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)  # T-60s
p.write_text(json.dumps(d))
PY

# (No strace needed — file mutation alone proves refresh occurred.)
HASH_BEFORE=$(sha256sum ~/.claude/.credentials.json)
timeout 30 claude -p 'reply OK' < /dev/null
HASH_AFTER=$(sha256sum ~/.claude/.credentials.json)
[ "$HASH_BEFORE" = "$HASH_AFTER" ] && echo "no refresh" || echo "REFRESHED"
```

Outcome 2026-04-28T19:24Z: file changed, new accessToken + new refreshToken, expiresAt advanced 8h. Confirmed: host Claude refreshes proactively when local expiresAt is past.

**Caution if re-running:** if shared creds and host creds are converged at the time of the experiment (the bug being investigated), the rotation invalidates shared. Either run when they're known-diverged, or accept that you'll need to refresh shared via the broker afterwards (`echo '{"action":"refresh"}' framed | nc -U /tmp/yolo-claude-oauth-broker.sock` — but rt must be expired or it's a cache-hit). I learned this the hard way during this investigation: triggered host /login the user had to do.

## Recommended sequence

1. **Verify by inspection.** ✅ Confirmed 2026-04-30 by reading `entrypoint.py:1119-1158` and `cli.py:3590,7547`. Path is exactly as described in the handoff.
2. **Run the host strace.** ✅ Done 2026-04-28T19:24Z — outcome above. Host Claude refreshes proactively. Proactive refresher is NOT needed.
3. **Land the fix.** One-line removal of `.credentials.json` from `DEFAULT_HOST_CLAUDE_FILES`. Plus optional cleanups in `entrypoint.py`. Awaiting user sign-off.
4. **Document the migration** in CHANGELOG / release notes — same wording as `cb6e850`, since this finally lands what that commit promised.
5. ~~Decide on the proactive refresher.~~ Resolved by step 2: not needed.

## What I (the in-jail agent) shipped this session

- `3f976cd` fix(doctor): skip singleton broker in per-jail liveness probe
- `4dbbd20` feat(doctor): symptom-level shared-creds freshness check
- `b245f3f` docs(broker): align triage doc with broker era; record 2026-04-28 findings
- `7018784` feat(doctor): include shared-creds mtime as time-since-last-refresh proxy

All pushed (per user) on `main`.  The doctor changes will surface this bug visibly going forward — `shared creds last write Xh ago` lets you see exactly when something rewrote shared creds without having to inotify.

## What I deliberately did NOT do

- **Did not remove `.credentials.json` from `DEFAULT_HOST_CLAUDE_FILES`** despite identifying it as the bug.  This is a real architectural change, affects every user on first jail start after the upgrade, and should be reviewed.  Kicked to you.
- **Did not run the host strace.**  Can't run it from inside the jail; needs host access.
- **Did not commit a proactive refresher.**  Would be premature before the entrypoint sync is removed (its presence currently masks the question of whether Claude refreshes).

## Files touched in this session (all committed)

- `src/cli.py` — `_check_host_service_liveness` skips singleton broker; `_check_broker_creds_freshness` added.
- `tests/test_cli_unit.py` — `TestHostServiceLivenessProbe`, `TestBrokerCredsFreshness`.
- `docs/claude-token-logouts.md` — rewritten for broker era.
- `HANDOFF-credentials-logout-2026-04-28.md` — appended a 2026-04-28T21:30Z update with that day's findings.

## Files referenced (not modified)

- `src/entrypoint.py:1119` — the bug.
- `src/cli.py:3590` — the env var that drives it.
- `src/cli.py:7547` — where the env var is set on the docker invocation.
- `src/oauth_broker.py:_maybe_propagate_token_response` (~line 590) — the *other* writer of shared creds: broker mirrors `/login` responses.  Stays.
