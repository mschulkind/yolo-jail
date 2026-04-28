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

## Remaining unanswered: does Claude in the jail refresh at all?

Even after this fix, the observation that the broker proxy log has zero `is_refresh=True` entries is unexplained.  Two hypotheses:

1. **Claude does refresh, but our TLS terminator + broker log filtering loses it.** Falsifiable: instrument `oauth_broker_jail.py:_handle` to log every POST body length, regardless of `_is_refresh_grant` outcome.  Re-check after a jail has been running for >8h.
2. **Claude doesn't refresh at all (in any environment).** Falsifiable by host-side strace (next section).

Hypothesis 2 is testable on the host because the host has its own creds file, no MITM, no shared-identity issue.  If host Claude refreshes, hypothesis 1 holds and we should look for instrumentation gaps.  If host Claude does not refresh either, then the `/login` cadence on the host is purely user-driven (re-`/login` periodically because the host token expired), and the proactive refresher in the broker becomes load-bearing.

## Host-side experiment to run (the original ask)

The user asked for a host-vs-jail strace comparison. With the root cause now likely identified, the experiment's purpose narrows: **does host Claude emit `grant_type=refresh_token` on its own?**

```bash
# On host, with NO jail running.  Make a snapshot of host creds first:
cp ~/.claude/.credentials.json /tmp/creds-snapshot-$(date +%s).json

# Pre-expire the local expiresAt to force a refresh check.
# (server-side validity unchanged — this is a client-side hint only)
python3 - <<'PY'
import json, time, pathlib
p = pathlib.Path.home() / ".claude/.credentials.json"
d = json.loads(p.read_text())
d["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)  # T-60s
p.write_text(json.dumps(d))
print("primed expiresAt to T-60s")
PY

# Strace the actual binary (NOT the shim).  Adjust path as needed.
REAL_BIN="$HOME/.local/bin/claude"  # or wherever host claude lives
strace -f -y -s 200 -e trace=connect,sendto,write,openat \
   -o /tmp/claude-host.strace \
   "$REAL_BIN" -p 'reply OK' 2>&1 | head -20

# Look for refresh attempts:
grep -E 'grant_type|refresh_token|api.anthropic|platform.claude|/v1/oauth' \
   /tmp/claude-host.strace | head -30
```

**Read the result against:**
- Did Claude emit a POST containing `grant_type=refresh_token`?  → answers Q1.
- Did the access token in the file change after the run?  → confirms refresh succeeded.
- Did the refresh token rotate?  → expected.

If host Claude refreshes successfully, the bug is exclusively the entrypoint sync.  If it doesn't, we have a second bug: Claude has no proactive refresh and the broker's removed `02821aa` refresher needs to come back.

> **Don't run this experiment in the jail unless you're prepared to lose the jail's auth.**  A successful refresh rotates the upstream refresh token; the rotation invalidates whatever's in shared creds at that moment, and if shared creds were just synced from host (per the bug), the host loses auth too.  After the entrypoint fix lands, the experiment can be run safely in the jail because identities will actually be independent.

## Recommended sequence

1. **Verify by inspection.** Read `src/entrypoint.py:1119-1158` and `src/cli.py:3590,7547`. Confirm the path described above. (5 min.)
2. **Run the host strace** above to answer the secondary question (does host Claude refresh?).  Keep the snapshot file in case the experiment needs reverting.
3. **Land the fix.** One-line removal of `.credentials.json` from `DEFAULT_HOST_CLAUDE_FILES`. With or without the cleanups. Tests: confirm an existing jail with valid shared creds boots cleanly without an entrypoint copy. Doctor's new `shared creds valid for Xh Ym, last write Yh ago` line stays unchanged.
4. **Document the migration** in CHANGELOG / release notes — same wording as `cb6e850` since this finally lands what that commit promised.
5. **Decide on the proactive refresher.** Only meaningful after #3 lands and we've watched a jail in the wild for >8h with no entrypoint sync masking the refresh question.  If shared creds genuinely don't get refreshed by Claude, bring back `02821aa`.  If they do, leave well enough alone and move on.

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
