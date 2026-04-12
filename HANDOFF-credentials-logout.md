# Handoff — Claude credential logout investigation

Investigator: continuation of RE work on why jails repeatedly log out of Claude after the read-only-home refactor. The user believes the problem began with commit `2bcc4e7` ("fix: Claude auth in jail and read-only home filesystem"). This doc captures everything learned so far and the open leads.

## Symptom

User gets logged out of Claude in jails **multiple times per day**. Didn't happen before the read-only spree. The observed error is `Please run /login · API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid authentication credentials"}}` — server-side rejection of the access token.

Evidence in waxon jail session: `~/code/waxon/.yolo/home/claude/projects/-workspace/8f644ee7-ec63-444e-9189-29b0e79f41a6.jsonl`, look for `isApiErrorMessage:true` / `"error":"authentication_failed"`.

## Architecture recap

Three credential files exist on the host:

| Path | Role | Current state (2026-04-12) |
|---|---|---|
| `~/.claude/.credentials.json` | Host Claude's own copy (used when running claude outside jail) | inode 12349688, mtime 06:59:40, expires 14:59:40 |
| `~/.local/share/yolo-jail/home/.claude/.credentials.json` | GLOBAL_HOME — bind-mount source shared across all jails | inode 21294959, mtime 07:06:14, expires 15:06:14 |
| `~/code/<proj>/.yolo/home/claude/.credentials.json` | Per-workspace overlay stub (0 bytes — mountpoint target) | 0 bytes, created at first jail boot, never touched again |

Mount layout inside a running jail (from `src/cli.py:4187-4244`):
- `/home/agent` ← GLOBAL_HOME, mounted `:ro`
- `/home/agent/.claude` ← per-workspace `ws_state/claude` dir, rw (overlays on top of `:ro` base)
- `/home/agent/.claude/.credentials.json` ← `GLOBAL_HOME/.claude/.credentials.json` single-file bind mount, rw (overlays on top of the per-workspace dir overlay — see `src/cli.py:4239`)

So the per-workspace `.yolo/home/claude/.credentials.json` exists only as a mountpoint target; at runtime it's shadowed by the bind-mounted GLOBAL_HOME file.

The entrypoint's `_sync_host_claude_files()` in `src/entrypoint.py:964-997` runs on every jail boot and copies `host ~/.claude/.credentials.json` → `GLOBAL_HOME/.claude/.credentials.json` (via the in-jail bind mount path) iff the host has a later `expiresAt`. It uses `shutil.copy2` with a SameFileError fallback. `_credentials_expiry` (line 952) returns 0 on parse failure.

## Empirical findings

1. **Rename onto a single-file bind mount fails with `EBUSY` on this kernel.** Reproduced with `mount --bind host_file container_file; mv new_file container_file` → "Device or resource busy". In-place writes (`echo x > container_file`) work fine — same inode, content updates, both paths agree.
2. **Two live jails share credential updates for `/login`.** User ran test: logged-out jail A + logged-out jail B, `/login` in A → B was immediately logged in. Proves `/login` persists through the bind mount live.
3. **GLOBAL_HOME credentials inode is stable across reads** (21294959 observed multiple times over ~30min window), yet `mtime` advances (was `Apr 11 20:27` right after login, now `07:06:14` today). **This means refreshes are happening and persisting in-place — the file is being rewritten via `open(O_TRUNC)+write`, not rename.** So refresh IS working.
4. **GLOBAL_HOME and host have diverged tokens.** Different inodes. Host expires at 14:59, GLOBAL_HOME at 15:06 — 7 minutes apart. Each was refreshed independently against the auth server. Confirms the read-only refactor fully separated host-Claude and jail-Claude credential state.

## Claude Code write logic (reversed from `/home/matt/.local/share/claude/versions/2.1.104`)

The binary is a stripped ELF bundled with Bun. Use `strings` and `dd` with byte offsets. Key functions (obfuscated names but identifiable by surrounding literals):

### Atomic write helper `IWH` (near the strings `Writing to temp file:`, `Falling back to non-atomic write for`, `tengu_atomic_write_error`)

```js
function IWH(path, data, opts={encoding:"utf-8"}) {
  let target = path;
  try { target = readlinkSync(target) /* resolved */ } catch {}
  const tmp = `${target}.tmp.${process.pid}.${Date.now()}`;
  let mode, haveMode = false;
  try { mode = statSync(target).mode; haveMode = true; } catch (e) { /* ENOENT ok */ }
  try {
    writeFileSync(tmp, data, {encoding, flush: true});
    if (haveMode) chmodSync(tmp, mode);
    renameSync(tmp, target);                     // fails EBUSY in jail
  } catch (err) {
    emit("tengu_atomic_write_error", {});
    try { unlinkSync(tmp); } catch {}
    writeFileSync(target, data, {encoding, flush: true}); // fallback — in-place
  }
}
```

Fallback path explains how refreshes can hit the same inode: rename fails, falls back to in-place truncate+write.

### Plaintext credential provider `XCq` (offset ~110141500)

```js
function Cv$() { return { storageDir: q6(), storagePath: join(q6(), ".credentials.json") }; }
XCq = {
  name: "plaintext",
  read() { try { return JSON.parse(readFileSync(Cv$().storagePath, "utf8")); } catch { return null; } },
  async readAsync() { /* same, async */ },
  // WRITE METHOD NOT YET LOCATED — needs more disassembly
  // Called as `$.update(K)` from the save function ~110142000
};
```

**Critical open question**: does `XCq.update` (the write) go through `IWH` or does it call `writeFileSync`/`writeFile` directly? If the latter, it only uses plain in-place writes and the "rename fallback" theory above is irrelevant — but then we also don't have a failure mode to explain the logouts.

### OAuth refresh flow `my8` / `Hf` (offset ~110142800)

Call flow:
1. `Hf(0, false)` → entry (with dedup via `PiH` promise)
2. `my8(H, $)`:
   - `await ak4()` — reinitialize cache based on file mtime check
   - Check `refreshToken` exists and expiry is soon (`Eg(expiresAt)`)
   - `mkdir(storageDir, {recursive: true})`
   - **Acquire file lock via `vw(A)`** where `A = q6() = storageDir`. `vw` is likely `proper-lockfile` (emits `tengu_oauth_token_refresh_lock_acquiring`, `_acquired`, `_retry`, `_retry_limit_reached`, `_error`, `_releasing`, `_released`).
   - `BQH(refreshToken, ...)` — network call to refresh endpoint
   - `QTH(newTokens)` — save via the plaintext provider (calls `$.update(K)`)
3. Telemetry events fired: `tengu_oauth_token_refresh_starting`, `tengu_oauth_token_refresh_race_resolved`, `tengu_oauth_token_refresh_race_recovered`, `tengu_oauth_tokens_saved`, `tengu_oauth_tokens_save_failed`, `tengu_oauth_tokens_save_exception`, `tengu_oauth_401_recovered_from_keychain`, `tengu_oauth_401_sdk_callback_refreshed`

Bytes ~110142360 show the write wrapper:
```js
// ... $.update(K) where K is the new token object ...
if (A.success) c("tengu_oauth_tokens_saved", {storageBackend: q});
else c("tengu_oauth_tokens_save_failed", {storageBackend: q});
```

### Separate auth-loss guard for `~/.claude.json` (not `.credentials.json`)

Offsets 110348521, 110351439, 110354996 — `tengu_config_auth_loss_prevented`. Protects `~/.claude.json` (the config file with `oauthAccount`, `hasCompletedOnboarding`, etc.) from being written with missing-auth when the cache still has auth. Error message: `"saveConfigWithLock: re-read config is missing auth that cache has; refusing to write to avoid wiping ~/.claude.json. See GH #3117."`

This is about **a different file** than credentials. But note: the yolo-jail symlinks `~/.claude.json` → `.claude/claude.json` (per-workspace overlay, `src/cli.py:382-385`). The symlink target is in a dir overlay, so atomic rename should work for it. Verify anyway.

## What's ruled out

- **"Rename silently breaks in jail"** as sole cause — empirically refreshes are landing in GLOBAL_HOME (mtime advances, inode stable). Fallback is working **or** refresh write path doesn't use rename at all.
- **"`/login` bind mount is broken"** — user's two-jail live test proves updates propagate.
- **`_sync_host_claude_files` clobbering fresher GLOBAL_HOME** — expiry comparison logic is `dst_expiry >= src_expiry and dst_expiry > 0`; skips if dst is fresh and valid. Safe on the happy path.
- **Per-workspace overlay file being used** — it's shadowed by the bind mount at runtime, never read while the jail is up. The 0-byte stub is benign.

## Unexplained — what to investigate next

The core mystery: if refreshes are persisting (they are, GLOBAL_HOME mtime moves), why does the user get 401s and re-login multiple times per day?

Leading suspects, in priority order:

### 1. Locate `XCq.update` — see whether it uses `IWH` or direct writes

If `XCq.update` writes via `writeFileSync` directly (no tmp+rename), then there's no fallback path. A bind-mounted file write would still succeed (inode preserved). But then there's also no failure mode to explain the logouts. If it uses `IWH`, then refresh relies on the fallback — and `tengu_atomic_write_error` fires on every refresh, which is fine unless the fallback itself sometimes fails.

Approach: `dd if=claude bs=1 skip=110140000 count=4000 | strings` and walk forward. Search for the function referenced by `$.update(K)` at offset ~110142000. Alternatively, strace a real refresh (see #2).

### 2. Run a real refresh with strace + Claude debug logs

Force a near-expired token, then run `claude auth status` (or a cheap `-p "hi"`) under `strace -f -y -e trace=openat,rename,renameat,renameat2,unlink,write,flock`. Compare syscall sequence to the `IWH` pattern. Enable Claude debug logging (look for env vars — the binary shows `N("Writing to temp file: ${A}")` which is a debug-level log; likely controlled by `DEBUG` or `CLAUDE_CODE_DEBUG`).

Concretely: back up `~/.local/share/yolo-jail/home/.claude/.credentials.json`, edit `expiresAt` to `Date.now() + 60000` (1 minute out), start a jail and immediately `claude auth status`. Strace should show whether `renameSync` is attempted.

### 3. Check whether refresh lockfile is also on the bind-mounted path

`vw(A)` with `A = storageDir` (== `q6()`) suggests the lockfile lives next to `.credentials.json`. If it's in the `.claude/` dir directly (per-workspace overlay), each jail has its own lock → **no cross-jail mutual exclusion** → two jails can race and both refresh, with second-writer winning. Server may invalidate tokens if both try to use the same refresh token simultaneously (refresh tokens are usually single-use).

Find `q6()` and see what dir it returns. Check for `.credentials.json.lock`, `.lock`, `proper-lockfile` directory patterns. Empirically: run `watch -n0.5 'ls -la ~/.local/share/yolo-jail/home/.claude/'` for a while, look for transient lock files.

### 4. Check `tengu_*` telemetry persistence

Search for where tengu events are persisted on disk — the strings output showed a retry/queue system writing to `eiH()`/`ricH()` paths. If we can find the queue dir, `tengu_oauth_tokens_save_failed`, `tengu_oauth_token_refresh_lock_retry_limit_reached`, `tengu_atomic_write_error` events would confirm or deny failure hypotheses. Grep strings for `otel`, `events`, `.json` near tengu handling.

### 5. Audit `_sync_host_claude_files` under concurrent read

`shutil.copy2` on Linux uses `copyfile` → `copyfileobj` → open dst `wb` (truncates) → stream. During the truncate window, `dst` is 0 bytes. If another jail reads during that ms, it sees 0 bytes → parse fail → `XCq.read()` returns `null` → Claude may think "not logged in".

Frequency: one sync per jail boot, truncate window ~µs. User boots jails N times/day. Low but not zero probability. Mitigate: sync writes via rename on the host side (outside any bind mount), not through the container.

### 6. `tengu_config_auth_loss_prevented` — is `~/.claude.json` being wiped?

Even though credentials are fresh, Claude gates "logged in" on `Y$().oauthAccount` (visible in the binary slice: `function OA() { return Ej() ? Y$().oauthAccount : void 0; }`). `Y$` reads `~/.claude.json`. If that file loses `oauthAccount` due to a concurrent write race (the guard prevents it under `saveConfigWithLock`, but `IWH` itself doesn't re-read), Claude would show "logged out" despite valid credentials.

Check: `python3 -c "import json;print(json.load(open('~/.local/share/yolo-jail/home/.claude/claude.json')).get('oauthAccount'))"`. Also diff `.yolo/home/claude/claude.json` across projects to see if any have lost the field.

## Reproducing the test environment

```bash
# Current mtimes/inodes
stat -c '%n inode=%i mtime="%y" size=%s' \
  ~/.claude/.credentials.json \
  ~/.local/share/yolo-jail/home/.claude/.credentials.json

# Decode expiresAt (helper script already on disk)
python3 /tmp/creds_probe.py

# Watch for inode/mtime changes during a live jail session
watch -n 1 'stat -c "inode=%i mtime=%Y size=%s" ~/.local/share/yolo-jail/home/.claude/.credentials.json'

# Inspect the Claude binary
strings /home/matt/.local/share/claude/versions/2.1.104 | grep -E 'pattern'
dd if=/home/matt/.local/share/claude/versions/2.1.104 bs=1 skip=OFFSET count=3000 2>/dev/null | strings

# Bind-mount rename test (needs sudo)
# See /tmp/claude-strace.log-style experiment — run in a tmpdir:
# mount --bind host_file container_file; mv tmp container_file → EBUSY
```

## Key byte offsets in claude 2.1.104 binary

| Offset | Content |
|---|---|
| ~110125952 | `Cv$()` — storage path resolver for `.credentials.json` |
| ~110141500 | `XCq` plaintext storage provider (read/readAsync defined; update TBD) |
| ~110142000 | `$.update(K)` call site + `tengu_oauth_tokens_saved/_failed/_exception` |
| ~110142360 | `ak4()` mtime check + `sk4()` 401 recovery path |
| ~110142800 | `Hf()` / `my8()` — OAuth refresh with lockfile |
| ~110348521 | `tengu_config_auth_loss_prevented` + "refusing to write" message (saveGlobalConfig) |
| ~110351439 | same for `saveConfigWithLock`, mentions `~/.claude.json` and GH #3117 |
| ~110354996 | same for `saveCurrentProjectConfig` |

Search with: `grep -boaE 'pattern' /home/matt/.local/share/claude/versions/2.1.104`. Offsets appear duplicated in the binary (two code regions) — probably original + hot-path copy from Bun bundling.

## Relevant yolo-jail source paths

- `src/cli.py:4187-4260` — jail docker run command, all mount specs
- `src/cli.py:4239` — the credentials single-file bind mount (prime suspect for the root-cause layer)
- `src/cli.py:4135-4167` — `_seed_agent_dir` (explicitly skips `.credentials.json`) and `.claude.json` onboarding merge
- `src/cli.py:338-398` — `ensure_global_storage` (creates file mountpoints including `.claude/.credentials.json`, plus the `.claude.json` symlink hack)
- `src/entrypoint.py:952-997` — `_credentials_expiry` and `_sync_host_claude_files` (runs on every jail boot, copies host → GLOBAL_HOME on expiry advantage)
- `src/entrypoint.py:1064` — call site of `_sync_host_claude_files`

## Out-of-scope but related

- Commit `2bcc4e7` is the suspected regression origin. `git show 2bcc4e7 -- src/cli.py src/entrypoint.py` for the full pre/post diff.
- Pre-refactor, `.claude/` was a rw overlay directory with no file-level mount on `.credentials.json`. Credentials were per-workspace, not shared. Refactor introduced the shared bind mount to enable cross-jail `/login`.
- `.gitconfig` and `.bashrc` already use **symlinks into writable overlays** as a workaround for the `:ro`-base + atomic-write problem (commit `8302237`). Credentials file doesn't — that's inconsistent and may be the right fix: symlink-based sharing instead of file bind mount.

## Recommended fix direction (not yet committed)

Replace the single-file bind mount at `src/cli.py:4239` with a **directory-level shared mount** + symlink into the per-workspace `.claude/` overlay:

1. Create a dedicated shared dir: `GLOBAL_STORAGE/shared-credentials/.credentials.json` on the host.
2. Bind-mount the parent dir `GLOBAL_STORAGE/shared-credentials` → `/home/agent/.claude-shared` rw.
3. In the entrypoint, symlink `/home/agent/.claude/.credentials.json` → `/home/agent/.claude-shared/.credentials.json`.
4. Claude's `IWH` resolves the symlink before writing (`readlinkSync` at the top of `IWH`), so tmp+rename happens in `/home/agent/.claude-shared/`, which is a directory bind mount where rename works fine.
5. Delete `_sync_host_claude_files`' `.credentials.json` special case; either drop the file from `DEFAULT_HOST_CLAUDE_FILES` entirely or handle first-boot seed via a plain copy.

This matches the pattern already used for `.claude.json` (symlink into writable overlay). Confirm it fixes the logout before committing. **Don't ship until the root cause is actually identified** — if the issue is something else (e.g. #6 above), this fix won't help.

## Scratch artifacts left on disk

- `/tmp/creds_probe.py` — expiresAt decoder
- `/tmp/claude-strace.log` — strace of `claude --help` (no credential access — not useful, can delete)
- `/tmp/claude-auth-strace.log` — strace of `claude auth status` (read-only credential access, useful baseline)
- `/tmp/claude-help.txt` — help text dump
- `/home/matt/.claude/projects/-home-matt-code-yolo-jail/503e4afa-eb67-4678-a146-9ae0ab37b552/tool-results/b*.txt` — saved strings/grep outputs from this session
