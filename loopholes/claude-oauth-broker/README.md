# claude-oauth-broker loophole

Reference implementation of a yolo-jail loophole. MITM proxy that terminates TLS for `platform.claude.com` on the host, serializes every OAuth refresh through a flock, and hands jails a cached access token when one is still valid. See [`docs/claude-oauth-mitm-proxy-plan.md`](../../docs/claude-oauth-mitm-proxy-plan.md) for the full design, [`docs/claude-token-logouts.md`](../../docs/claude-token-logouts.md) for how this fits in the overall logout triage, and [`docs/loopholes.md`](../../docs/loopholes.md) for the plugin system.

## Files

| File | Purpose |
|---|---|
| `manifest.jsonc` | Loophole manifest. Installed under `~/.local/share/yolo-jail/loopholes/claude-oauth-broker/` by `just deploy`. |
| `claude-oauth-broker.service` | systemd user unit template. `@@BROKER_BIN@@` is substituted at deploy time with the console script's absolute path. |
| `ca.crt`, `ca.key` | Generated on first run by `yolo-claude-oauth-broker --init-ca`. Root CA is valid 10 years; jails trust it via `NODE_EXTRA_CA_CERTS`. Never checked into git. |
| `server.crt`, `server.key` | Leaf cert for `platform.claude.com`, issued by the CA. Also generated on first run. |

## Install

`just deploy` handles everything:

1. Installs the yolo-jail wheel (gives you `yolo-claude-oauth-broker` on PATH).
2. Copies this directory to `~/.local/share/yolo-jail/loopholes/claude-oauth-broker/`.
3. Runs `yolo-claude-oauth-broker --init-ca` to generate the CA/leaf pair.
4. Templates the systemd unit and starts `claude-oauth-broker.service`.

## How the network path works

Claude Code inside a jail opens TLS to `platform.claude.com`. The loophole's manifest routes that hostname to `host-gateway`, which podman/docker translates to whatever host address the container can actually reach — `169.254.1.2` on pasta-rootless podman, the bridge gateway on CNI/Docker, a VM gateway on Docker Desktop. Traffic arrives on the host's loopback, where the broker listens on `0.0.0.0:443` and accepts it.

We deliberately don't pin a literal IP in the systemd unit or the manifest; pinning `169.254.1.2` only works on pasta, and the failure mode is subtle — the daemon crash-loops with `EADDRNOTAVAIL` because that address isn't on any real host interface.

## Port 443 requirement

Binding port 443 is privileged. The default:

- **`AmbientCapabilities=CAP_NET_BIND_SERVICE`** in the systemd unit. Works on most modern systemd setups; some restrictive user-namespace configurations disallow ambient caps and you'll need a fallback.

If that fails (journal says `Failed to bind to port 443 (Permission denied)`), pick one:

- **`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0`** (persist in `/etc/sysctl.d/99-yolo.conf`). Global, lets any user bind any port. Minimal privilege increase in practice — port numbers carry no special meaning today — but requires a one-time sudo.
- **DNAT on the container bridge.** Redirect the host-side flow from `:443` → `:8443` via iptables/nftables, then edit the unit's `ExecStart` to `--port 8443`. Requires sudo at deploy time and correct matching on the bridge interface (`-i podman0` / `-i docker0`).

`yolo doctor` surfaces which option you need: when the broker is stuck in `activating`, it scans the service journal for the common failure signatures and prints the specific remediation.

## Operations

```bash
# Status
systemctl --user status claude-oauth-broker
journalctl --user -u claude-oauth-broker -n 50 --no-pager

# Health check (also wired into `yolo doctor` via manifest.doctor_cmd)
yolo-claude-oauth-broker --self-check

# Regenerate CA/leaf (breaks all existing jails until they restart and re-read NODE_EXTRA_CA_CERTS)
yolo-claude-oauth-broker --force-init-ca
systemctl --user restart claude-oauth-broker
```

## Disable

Set `"enabled": false` in `~/.local/share/yolo-jail/loopholes/claude-oauth-broker/manifest.jsonc` (or `yolo loopholes disable claude-oauth-broker`) and stop the service:

```bash
systemctl --user disable --now claude-oauth-broker
```

The refresher (`claude-token-refresher`) remains a valid fallback — with the broker disabled, jails fall back to the single-writer refresher story and occasionally race.

## Interaction with the refresher

The broker and the refresher can coexist:

- Broker handles **real-time** refresh requests from jails (synchronous).
- Refresher runs on a timer and proactively keeps the shared file ahead of expiry (eager).

Running both is safe — the broker's flock serializes against itself, and the refresher's flock is separate but refreshes are idempotent at the file level. If you want broker-only, set `claude_token_refresher: false` in `~/.config/yolo-jail/config.jsonc`.

## Writing your own loophole

The schema lives in [`src/loopholes.py`](../../src/loopholes.py) — docstring at the top. A new loophole is a directory with:

- `manifest.jsonc` (required)
- `ca.crt` (optional, auto-trusted if present)
- systemd unit / launchd plist / whatever the daemon needs (the loophole owns its own lifecycle)

Drop it under `~/.local/share/yolo-jail/loopholes/<name>/`, make sure the manifest's `name` field matches the directory name, and it gets picked up at next `yolo run`. No core changes required.

For `unix-socket` + `spawned` loopholes (like host-processes), the short path is adding a `host_services` entry in `yolo-jail.jsonc` and building the daemon with the `src/host_service.py` helper library. See [`docs/loopholes.md`](../../docs/loopholes.md).
