# yolo-jail loopholes

A **loophole** is a single controlled permeability point between the jail and the host — a sanctioned narrow passage through the wall. The jail talks to something through the loophole, and nothing escapes that's not declared.

Examples:

- [`claude-oauth-broker`](../loopholes/claude-oauth-broker/) — MITM proxy that serializes Claude OAuth refreshes (transport: `tls-intercept`, lifecycle: `external`).
- `host-processes` — allowlisted read-only view of host processes (transport: `unix-socket`, lifecycle: `spawned`).
- `journal`, `cgroup-delegate` — built-in loopholes surfaced from `loopholes` in `yolo-jail.jsonc`.
- Hypothetical future: `llm-audit` (logs every inference request), `secret-gate` (scrubs outbound traffic).

## Anatomy of a file-backed loophole

```
~/.local/share/yolo-jail/loopholes/<name>/
├── manifest.jsonc          # required
├── ca.crt                  # optional; auto-trusted in the jail
├── <your-daemon>.service   # optional; loophole owns its own lifecycle
└── README.md               # optional; for operators
```

Only `manifest.jsonc` is required. Everything else is up to the loophole.

## Manifest schema (v1)

```jsonc
{
  "name": "my-loophole",          // required; must match directory name
  "description": "…",             // required; one-line human summary
  "version": 1,                   // manifest format; currently 1
  "enabled": true,                // default true; toggle via CLI
  "transport": "tls-intercept",   // or "unix-socket" or "none"
  "lifecycle": "external",        // or "spawned" (yolo manages the daemon)
  "intercepts": [                 // tls-intercept only
    {"host": "example.com"}
  ],
  "broker_ip": "host-gateway",    // tls-intercept only; podman/docker magic value
  "ca_cert": "ca.crt",            // tls-intercept only; auto-mounted + trusted
  "jail_env": {"FOO": "bar"},     // any transport
  "doctor_cmd": ["bin", "--ok"]   // optional; run by `yolo doctor`
}
```

What the loader does at each `yolo run`:

1. Scans `~/.local/share/yolo-jail/loopholes/` for subdirectories with a valid `manifest.jsonc`.
2. Skips any with `"enabled": false`.
3. For `tls-intercept` loopholes: emits `--add-host <host>:<broker_ip>` for each intercept, bind-mounts the CA cert into the jail at `/etc/yolo-jail/loopholes/<name>/ca.crt`, and sets `NODE_EXTRA_CA_CERTS` to all loophole CAs concatenated. **Note:** Apple Container (`runtime=container`) does not support `--add-host`; intercept DNS entries are skipped with a warning. CA mounts, `jail_env`, and other wiring still apply.
4. For `unix-socket` / `spawned` loopholes — declare them via the `loopholes` shorthand in `yolo-jail.jsonc`; yolo handles spawning the daemon, creating the socket, bind-mounting it into the jail, and cleanup.
5. Merges `jail_env` into the container env.

Invalid manifests are skipped silently at runtime; `yolo loopholes list` surfaces the error.

## `loopholes` in `yolo-jail.jsonc`

The `loopholes` block is the workspace-scoped entry point. Each entry is treated as a `unix-socket` + `spawned` loophole — yolo spawns the daemon process at jail startup, creates a Unix socket, bind-mounts it into the jail, and tears down on exit. They appear in `yolo loopholes list` alongside file-backed loopholes so the whole picture lives in one command.

```jsonc
"loopholes": {
  "host-processes": {
    "description": "Allowlisted view of host processes",
    "command": ["yolo-host-processes", "--socket", "$SOCKET"],
    "doctor_cmd": ["yolo-host-processes", "--self-check"]
  }
}
```

Writing the daemon: use the [`src.host_service`](../src/host_service.py) helper library (see below).

## CLI

```bash
yolo loopholes list              # show every loophole, transport, enabled state
yolo loopholes status            # run every doctor_cmd
yolo loopholes enable <name>     # flip `enabled` → true (file-backed only)
yolo loopholes disable <name>    # flip `enabled` → false
yolo doctor                      # includes loophole self-checks in the combined report
```

## The `host_service` helper library

Writing a `unix-socket`/`spawned` loophole used to mean reimplementing the frame protocol, signal handling, the bind/umask dance, per-connection threading, and structured logging. The library takes that off your plate. The whole API is `serve(handler)` + `Session`:

```python
from src.host_service import serve, Session

ALLOWED_COMMS = {"layout-manager", "sway"}

def handle(session: Session) -> None:
    comm = session.request.get("comm")
    if comm not in ALLOWED_COMMS:
        session.stderr(f"comm {comm!r} not allowlisted\n")
        session.exit(2)
        return
    session.exec_allowlisted(
        lambda req: ["ps", "-o", "pid,comm,args", "-C", req["comm"]],
        allowlist=ALLOWED_COMMS,
    )

if __name__ == "__main__":
    import sys
    from pathlib import Path
    serve(handle, socket_path=Path(sys.argv[1]))
```

The library takes care of:

- **Frame protocol v1** — see [`docs/loophole-protocol.md`](loophole-protocol.md).
- **Access logging** — one structured line per request (jail id, request keys, elapsed, bytes out). No opt-in.
- **Command-injection guard** — `Session.exec_allowlisted(argv_builder, allowlist=…)` validates argv strings against a server-owned allowlist before invoking the subprocess. Daemons that skip this and shell out manually are on their own; the helper makes the safe path the short path.
- **JSON output convenience** — `session.json(obj)` emits one newline-terminated JSON line on stdout. Agents parse JSON; humans can use `--table` on the client side.
- **Signal-safe teardown** — SIGTERM / SIGINT shut down the accept loop cleanly, the socket is removed on exit.
- **Thread-per-connection** — cheap, stdlib-only.

External projects that want the library add `yolo-jail` to their deps and import `from src.host_service import serve`.

## Example: adding a minimal smoke-test loophole

```bash
mkdir -p ~/.local/share/yolo-jail/loopholes/hello
cat > ~/.local/share/yolo-jail/loopholes/hello/manifest.jsonc <<'EOF'
{
  "name": "hello",
  "description": "Smoke test — injects HELLO=world into every jail",
  "version": 1,
  "transport": "none",
  "jail_env": {"HELLO": "world"}
}
EOF
yolo loopholes list                # => enabled  hello  (none/external)
yolo -- bash -c 'echo $HELLO'     # => world
```

Remove the directory to uninstall. No state lives outside it.

## Discovery from inside the jail

Agents inside the jail shouldn't need the briefing to enumerate every capability; the briefing instead points at the discovery command:

- `yolo loopholes list` — what's active and reachable from here.

Keeps the briefing tight and prevents drift when loopholes come and go.

## See also

- [`docs/loophole-protocol.md`](loophole-protocol.md) — wire protocol spec.
- [`loopholes/claude-oauth-broker/`](../loopholes/claude-oauth-broker/) — reference `tls-intercept` implementation.
- [`src/loopholes.py`](../src/loopholes.py) — loader source (docstring has the canonical schema).
- [`src/host_service.py`](../src/host_service.py) — helper library.
- [`src/host_processes.py`](../src/host_processes.py) — reference `unix-socket` consumer of the library.
- [`docs/claude-oauth-mitm-proxy-plan.md`](claude-oauth-mitm-proxy-plan.md) — design notes that shaped this architecture.
- [`docs/claude-token-logouts.md`](claude-token-logouts.md) — operational triage for Claude logouts; the broker loophole is Step 3's fix.
