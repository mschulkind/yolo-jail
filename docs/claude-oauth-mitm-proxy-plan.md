# Claude OAuth MITM Proxy — Deferred Plan

This doc captures the MITM-proxy approach to the Claude Code logout problem. It's the **fallback** if the periodic refresher (see `scripts/claude-token-refresher.py`) doesn't eliminate the 401 races in practice.

## Why this exists

The root cause of the repeated `/login` prompts is **OAuth refresh-token rotation**: each refresh on the Anthropic auth server invalidates the prior refresh token. When multiple jails share one credentials file and decide to refresh in the same window, only the first one wins — the rest present an already-burned refresh token and get 401. See `HANDOFF-credentials-logout.md` for the full investigation.

The periodic refresher solves this by making the host the only entity that ever refreshes, so jails never race. But it depends on a couple of assumptions about Claude Code's behavior (mainly: that `ak4()` re-reads `.credentials.json` from disk before hitting the network on refresh). If that assumption is wrong, jails will still race even with the refresher running.

In that case, the next step is intercepting the refresh at the HTTP layer — an MITM proxy on the host that every jail's Claude Code routes through.

## Why we can't just use CLAUDE_CODE_CUSTOM_OAUTH_URL

Reverse engineering the 2.1.101 binary (offsets 108256900–108258400) showed:

```js
function sy6() { return "prod" }   // hardcoded, no env var reads this

function n38() {
  if (process.env.CLAUDE_CODE_CUSTOM_OAUTH_URL) return "-custom-oauth";
  switch (sy6()) {
    case "local":   return "-local-oauth";
    case "staging": return "-staging-oauth";
    case "prod":    return "";
  }
}

// BN_ is the allowlist for CLAUDE_CODE_CUSTOM_OAUTH_URL
BN_ = [
  "https://beacon.claude-ai.staging.ant.dev",
  "https://claude.fedstart.com",
  "https://claude-staging.fedstart.com",
];

function r6() {
  // ... picks prod config ...
  const $ = process.env.CLAUDE_CODE_CUSTOM_OAUTH_URL;
  if ($) {
    const K = $.replace(/\/$/, "");
    if (!BN_.includes(K)) throw Error("CLAUDE_CODE_CUSTOM_OAUTH_URL is not an approved endpoint.");
    // override TOKEN_URL, CONSOLE_AUTHORIZE_URL, etc. with K
  }
  // ...
}
```

**Blockers:**

1. `CLAUDE_CODE_CUSTOM_OAUTH_URL` is allowlisted to three hard-coded URLs (FedRAMP / staging). localhost is rejected at startup with an error.
2. `sy6()` is compiled to always return `"prod"`, so the `"local"` case (which would read `CLAUDE_LOCAL_OAUTH_API_BASE`, `CLAUDE_LOCAL_OAUTH_APPS_BASE`, `CLAUDE_LOCAL_OAUTH_CONSOLE_BASE` and point at localhost) is unreachable.
3. Patching the `BN_` allowlist inside the binary would work but would need to be re-applied on every Claude Code update — brittle.

So we can't redirect OAuth via env vars. If we want to intercept, we have to terminate TLS.

## Real OAuth endpoints (from the binary)

```
BASE_API_URL:           https://api.anthropic.com
TOKEN_URL:              https://platform.claude.com/v1/oauth/token    ← refresh lives here
CONSOLE_AUTHORIZE_URL:  https://platform.claude.com/oauth/authorize
CLAUDE_AI_AUTHORIZE_URL: https://claude.com/cai/oauth/authorize
API_KEY_URL:            https://api.anthropic.com/api/oauth/claude_cli/create_api_key
ROLES_URL:              https://api.anthropic.com/api/oauth/claude_cli/roles
CLIENT_ID:              9d1c250a-e61b-44d9-88ed-5944d1962f5e
OAUTH_BETA_HEADER:      oauth-2025-04-20
```

Note: multiple public sources (including earlier research during this investigation) say the OAuth endpoint is `https://console.anthropic.com/v1/oauth/token`. **That's out of date.** The 2.1.101 binary points at `platform.claude.com`. When implementing, double-check the current binary — Anthropic may move this again.

## Architecture

### Components

- **broker daemon** — long-running Python process on the host, listens on a TCP port reachable from jails (`host.containers.internal:<port>`, e.g., `169.254.1.2:8443`).
- **CA** — self-signed root cert generated once, stored at `~/.local/share/yolo-jail/oauth-broker/ca.{crt,key}` (chmod 600 on the key). Valid for 10 years. The broker issues a cert for `platform.claude.com` signed by this root and serves it on 8443.
- **credentials state** — the broker owns `~/.local/share/yolo-jail/home/.claude/.credentials.json`. Still the same file that jails currently bind-mount, but the broker is the only writer.
- **lock** — `~/.local/share/yolo-jail/oauth-broker/refresh.lock` (flock). Serializes refreshes even if multiple broker requests arrive simultaneously.

### Request flow

1. Jail's Claude Code needs to refresh → opens TLS to `platform.claude.com:443`
2. Jail DNS override (`/etc/hosts` entry `169.254.1.2 platform.claude.com`) routes the connection to the broker
3. Broker accepts the TLS handshake using its issued cert (trusted via `NODE_EXTRA_CA_CERTS` in the jail)
4. Broker sees `POST /v1/oauth/token`
5. Broker acquires flock, checks if a valid access token is already in its in-memory cache (or on disk with a fresh `expiresAt`), and:
   - **Cache hit:** returns the cached token immediately without calling upstream — no race, all jails get the same token
   - **Cache miss:** does the real refresh against `https://platform.claude.com/v1/oauth/token` with the current refresh token, writes the new credentials to disk, updates the cache, returns to jail
6. For any other path on `platform.claude.com` (`/oauth/authorize`, etc.): reverse-proxy to the real host so `/login` etc. keep working
7. Broker releases flock

### Jail-side changes (per jail)

- Bind-mount `~/.local/share/yolo-jail/oauth-broker/ca.crt` to `/etc/claude-broker-ca.crt:ro`
- Set `NODE_EXTRA_CA_CERTS=/etc/claude-broker-ca.crt`
- Add `169.254.1.2 platform.claude.com` to `/etc/hosts` (via `--add-host` or an entrypoint mutation)
- Keep the `.credentials.json` bind mount — the broker still writes to it, and Claude Code still reads it for the initial load
- No other env var changes

### Why DNS override instead of HTTPS_PROXY

- `HTTPS_PROXY` affects *all* outbound HTTPS, including `api.anthropic.com` inference traffic. We'd need to pass that through, which is extra complexity.
- DNS override touches only `platform.claude.com`. Inference traffic to `api.anthropic.com` is untouched.
- `/etc/hosts` is trivially scripted at jail start.

## Implementation checklist

- [ ] Generate CA on first run: 10-year RSA 4096 root, store in `~/.local/share/yolo-jail/oauth-broker/ca.{crt,key}`, `ca.key` chmod 600
- [ ] Issue leaf cert for `platform.claude.com` signed by the CA, with SANs for `platform.claude.com` and `localhost`
- [ ] Python broker using `aiohttp` or `http.server` + `ssl` — needs to do:
  - [ ] TLS termination with the leaf cert
  - [ ] Route `POST /v1/oauth/token` with `grant_type=refresh_token` to the cache/flock/refresh logic
  - [ ] Pass through every other path to real `platform.claude.com` (use `httpx` client with a fresh TCP connection so we don't trigger loops)
  - [ ] Lock via `fcntl.flock` on a dedicated lock file
  - [ ] Atomic in-place write of `.credentials.json` (no rename — rename onto a bind-mounted file fails with EBUSY)
- [ ] Systemd service + socket activation OR a simple always-on unit; socket activation would let the broker idle cheaply
- [ ] Update `src/cli.py` to:
  - [ ] Add `--add-host platform.claude.com:169.254.1.2` to the docker run command when the broker is running
  - [ ] Mount the CA cert into the jail
  - [ ] Set `NODE_EXTRA_CA_CERTS` env var
- [ ] Add a `yolo broker status` / `yolo broker logs` subcommand
- [ ] Tests:
  - [ ] Unit test: CA generation is idempotent
  - [ ] Unit test: refresh endpoint handling with a mock upstream
  - [ ] Integration test: real jail boot, real Claude Code, verify it hits the broker (use a fake CA + mock auth server)

## Risks and open questions

1. **Claude Code hardcodes `platform.claude.com` somewhere else too?** The binary has ~10 references. Most are the same config object, but some might be embedded in compiled code that resists env-var override. Verify by testing.
2. **Certificate pinning.** If Claude Code pins the `platform.claude.com` cert, MITM with a custom CA won't work. No evidence of pinning in the binary grep (no `tls.pinning`, no fingerprint constants near the TOKEN_URL), but worth confirming with a real test before committing.
3. **`oauth-2025-04-20` beta header.** The binary references this. If Anthropic expects it on refresh calls, the broker needs to forward it — shouldn't be a problem but easy to miss.
4. **`/login` flow.** Initial login uses the authorize URL (browser redirect), which the broker should pass through. Verify that login still works end-to-end with the broker in the path.
5. **CA rotation.** When the root CA nears expiry (10 years out), you need a rotation plan. Probably fine to punt for now.
6. **Multiple yolo-jail users on one machine.** Each user runs their own broker on a different port, or a shared system-wide broker. Currently scoped to single-user.
7. **nested jails.** A jail-inside-a-jail would need to reach the host's broker IP, which may not be routable. May need to either pass through the env var or run a broker per jail layer.

## References

- `HANDOFF-credentials-logout.md` — original investigation, symptoms, ruled-out hypotheses
- `scripts/claude-token-refresher.py` — the simpler refresher we're shipping first
- Prior art: `dyshay/proxyclawd` (MITM Claude Code with HTTPS_PROXY + NODE_EXTRA_CA_CERTS)
- Prior art: `griffinmartin/opencode-claude-auth` (in-process token broker pattern; same logic, no proxy)
- Upstream bugs: anthropics/claude-code#24317, #25609, #27933, #21765, #29896
