# Security Shim Architecture

YOLO Jail's security model borrows from two legendary examples of privilege separation:

- **xscreensaver**: Only a tiny, auditable piece of code runs with elevated privileges (the lock/unlock logic). All the complex, bug-prone code (screensaver animations) runs unprivileged in a separate process. The TCB (Trusted Computing Base) is small enough to read in an afternoon.

- **OpenSSH privsep**: A tiny privileged monitor process performs the handful of operations that require root. The entire protocol parser, key exchange, and user-facing code runs in an unprivileged, chrooted child. Communication is via a minimal, auditable IPC protocol. If the unprivileged side is fully compromised, the attacker still faces a second, much simpler barrier.

YOLO Jail applies the same principle: **all host-privileged code is concentrated in a single, small, auditable surface** — the "security shim" — while the vast majority of code (the entire container environment) runs with no special privileges.

---

## The Core Idea

```
┌─────────────────────────────────────────────────────────┐
│  UNTRUSTED SIDE (container)                             │
│                                                         │
│  Agent code, MCP servers, LSP, npm, pip, git,           │
│  entrypoint.py, shims, bootstrap, tools                 │
│                                                         │
│  Runs as unprivileged UID. No host credentials.         │
│  No writable cgroups. No device access.                 │
│  Cannot modify its own container config.                │
│                                                         │
│         │  Unix sockets (the ONLY bridge)  │            │
└─────────┼──────────────────────────────────┼────────────┘
          │                                  │
          ▼                                  ▼
┌─────────────────────────────────────────────────────────┐
│  SECURITY SHIM (host side — the entire TCB)             │
│                                                         │
│  1. Container lifecycle    (~100 lines)                 │
│  2. Cgroup delegate daemon (~300 lines)                 │
│  3. Port-forward bridge    (~80 lines)                  │
│  4. Config change gate     (~60 lines)                  │
│                                                         │
│  Total auditable surface: ~540 lines of Python          │
│                                                         │
│  Everything else in cli.py is config parsing,           │
│  validation, docs, and user interaction — it runs       │
│  BEFORE the container starts and has no ongoing         │
│  communication with the untrusted side.                 │
└─────────────────────────────────────────────────────────┘
```

The security shim is the **only code that bridges the trust boundary** between the container (untrusted) and the host (trusted). If you want to audit YOLO Jail's security, you audit ~540 lines. Everything else is either pre-launch setup or runs inside the sandbox.

---

## The Four Components

### 1. Container Lifecycle (~100 lines)

**What it does**: Constructs and executes the `docker run` / `podman run` command.

**Why it's privileged**: This is the moment where host resources (filesystem, devices, network) are granted to the container. The flags passed here define the entire sandbox boundary.

**How it's constrained**:

| Constraint | Mechanism |
|---|---|
| No arbitrary mounts | Config whitelist — only declared paths are mounted |
| No arbitrary capabilities | Hardcoded capability list (SYS_ADMIN only for Podman nesting) |
| No arbitrary devices | Device paths validated, USB resolved via lsusb |
| No silent config changes | Config change gate (component 4) blocks undeclared changes |
| No concurrent races | `fcntl.flock()` on workspace lock file |
| Arguments are not shell-injected | `subprocess` list mode (no `shell=True`) |

**Audit target**: The `docker_args` / `podman_args` list construction in `run()`. Every flag is visible, every mount is explicit.

### 2. Cgroup Delegate Daemon (~300 lines)

**What it does**: Runs a Unix socket server on the host that creates child cgroups, sets resource limits (CPU, memory, PIDs), and moves container processes into those cgroups.

**Why it's privileged**: The container's cgroup filesystem is mounted read-only (Podman rootless). Only the host can write to it. The daemon bridges this gap.

**How it's constrained**:

| Constraint | Mechanism |
|---|---|
| Caller identity is unforgeable | `SO_PEERCRED` — the kernel attests the PID of the connecting process. No tokens, no auth headers, no spoofable identity. |
| No path traversal | Cgroup names validated: `/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/` |
| No arbitrary cgroup writes | Only three controllers: `cpu`, `memory`, `pids` |
| CPU limits are bounded | 1% to 100×nproc (can't exceed physical capacity) |
| Memory limits are bounded | Minimum 1 MB (prevents OOM-kill loops) |
| PID limits are bounded | 1 to 1,000,000 |
| Every operation is logged | Request + response + peer PID written to audit log |
| Daemon dies with container | Lifecycle tied to container process |
| Protocol is tiny | 3 operations: `create_and_join`, `destroy`, `status` |

**The xscreensaver parallel**: Just as xscreensaver's lock/unlock code is a few hundred lines that do exactly one thing (authenticate and lock/unlock the X display), the cgroup daemon does exactly one thing (manage child cgroups within a container's cgroup). No general-purpose RPC, no shell execution, no file I/O.

**The OpenSSH parallel**: Just as OpenSSH's privileged monitor accepts a tiny set of messages (authenticate, allocate PTY, etc.) from the unprivileged child, the cgroup daemon accepts a tiny set of messages from the container. The protocol is a finite state machine small enough to enumerate exhaustively.

### 3. Port-Forward Bridge (~80 lines)

**What it does**: Creates socat processes that forward host `127.0.0.1` ports into the container via Unix socket tunneling.

**Why it's privileged**: The container can't reach host `127.0.0.1` through container networking (pasta/slirp4netns/bridge). The forwarding bridge bypasses this via bind-mounted Unix sockets.

**How it's constrained**:

| Constraint | Mechanism |
|---|---|
| Only declared ports | Port list comes from validated config (`network.forward_host_ports`) |
| Port range validated | 1–65535 |
| Only localhost | Forwards `127.0.0.1` only — no arbitrary host IPs |
| Cleanup on exit | All socat processes terminated when container exits |
| No network exposure | Unix sockets, not TCP — invisible to network scanners |

### 4. Config Change Gate (~60 lines)

**What it does**: Detects when `yolo-jail.jsonc` has been modified since the last launch and shows the operator a diff before proceeding.

**Why it matters**: An agent inside the jail could edit `yolo-jail.jsonc` (it's in the workspace mount) to add mounts, packages, or devices. Without this gate, the next jail launch would silently apply those changes.

**How it's constrained**:

| Constraint | Mechanism |
|---|---|
| Normalized comparison | Config is serialized to canonical JSON before diffing — whitespace/comment changes don't trigger |
| Human approval required | Interactive terminal: y/N prompt (default N) |
| Non-interactive safety | Piped stdin: auto-approves but prints a visible warning |
| Snapshot stored outside workspace | In `.yolo/config-snapshot.json` — agent can't delete it to bypass |

---

## What Is NOT in the Security Shim

Everything else in the codebase is either:

1. **Pre-launch setup** (config parsing, validation, image building, `yolo check`) — runs before the container exists, has no ongoing trust relationship with the untrusted side.

2. **Container-side code** (`entrypoint.py`, shims, bootstrap) — runs entirely inside the sandbox. Even if fully compromised, it can only affect the container.

3. **Documentation and CLI UX** — no security implications.

This separation means bugs in entrypoint.py, MCP server configs, LSP setup, shim generation, bashrc construction, or any tool installed inside the jail **cannot escalate to host privileges**. They can only affect the container environment.

---

## Trust Boundary Diagram

```
HOST (trusted)
  │
  ├── cli.py pre-launch
  │     ├── Parse & validate config
  │     ├── Build nix image (if changed)
  │     ├── Config change gate ◄── SHIM COMPONENT 4
  │     └── Load image into runtime
  │
  ├── cli.py runtime ◄── THE SECURITY SHIM
  │     ├── Container lifecycle ◄── SHIM COMPONENT 1
  │     │     └── docker/podman run (defines sandbox)
  │     ├── Port-forward bridge ◄── SHIM COMPONENT 3
  │     │     └── socat host↔container via Unix socket
  │     └── Cgroup delegate daemon ◄── SHIM COMPONENT 2
  │           └── Unix socket server for cgroup ops
  │
  ══════════════ TRUST BOUNDARY ════════════════
  │
CONTAINER (untrusted)
  │
  ├── entrypoint.py
  │     ├── Generate shims, bashrc, MCP/LSP configs
  │     ├── Bootstrap script (install tools)
  │     └── yolo-cglimit (Python socket client)
  │
  └── Agent (claude, copilot, gemini, user commands)
        ├── Full workspace read/write
        ├── Network egress
        ├── Tool installation (npm, pip, mise)
        └── NO: host credentials, cgroup writes,
             device access, config modification
```

---

## Audit Checklist

To verify YOLO Jail's security, audit these specific code sections:

### Container Lifecycle
- [ ] Every `-v` mount flag — are all host paths expected?
- [ ] Every `--cap-add` — is SYS_ADMIN only added for Podman nesting?
- [ ] Every `--device` — are device paths validated?
- [ ] `subprocess` call uses list mode, never `shell=True`?
- [ ] UID/GID mapping matches host user?

### Cgroup Delegate Daemon
- [ ] `SO_PEERCRED` is the only identity mechanism (no tokens)?
- [ ] Cgroup name regex rejects `..`, `/`, and other traversal?
- [ ] CPU/memory/PID limits have sane bounds?
- [ ] Only `cpu`, `memory`, `pids` controllers are enabled?
- [ ] Audit log captures every request?
- [ ] Daemon thread dies when container exits?

### Port-Forward Bridge
- [ ] Only `127.0.0.1` is forwarded (no `0.0.0.0`)?
- [ ] Port numbers are validated (1–65535)?
- [ ] socat processes are killed on container exit?
- [ ] Unix sockets are cleaned up?

### Config Change Gate
- [ ] Diff is shown before any privileged operation?
- [ ] Default answer is N (deny)?
- [ ] Snapshot is stored outside the workspace mount?

---

## Comparison with Prior Art

| Property | xscreensaver | OpenSSH privsep | YOLO Jail |
|---|---|---|---|
| **TCB size** | ~500 lines (lock/auth) | ~2000 lines (monitor) | ~540 lines (shim) |
| **Privilege mechanism** | setuid (optional) | root process | container runtime + cgroup writes |
| **IPC protocol** | X11 atoms | Unix pipe, fixed messages | Unix socket, JSON (3 operations) |
| **Identity verification** | PAM | SSH keys / PAM | SO_PEERCRED (kernel PID) |
| **Untrusted code** | Screensaver hacks | Protocol parser, key exchange | Agent, all tools, entrypoint |
| **Blast radius of compromise** | Ugly screensaver | No root escalation | No host escalation |
| **Audit strategy** | Read the lock code | Read the monitor | Read the 4 shim components |

---

## Design Principles

1. **Minimize the bridge**. The untrusted side communicates with the trusted side through exactly two Unix sockets (cgroup + port-forward) and zero other channels. Every other mount is either read-only or data (workspace files the agent is supposed to edit).

2. **Kernel-attested identity**. We never trust the container to identify itself. `SO_PEERCRED` gives us the host-namespace PID of the connecting process — this is set by the kernel, not by the caller. It's the Unix equivalent of a hardware attestation.

3. **Finite protocol**. The cgroup daemon accepts exactly 3 message types. The port-forward bridge has zero runtime messages (it's a static tunnel). There is no general-purpose RPC, no shell execution, no eval, no dynamic code loading across the trust boundary.

4. **Fail closed**. If the cgroup socket doesn't exist, `yolo-cglimit` prints an error and exits. If config validation fails, the container doesn't start. If the config change gate gets a non-interactive stdin, it warns loudly. The default answer to "apply these config changes?" is No.

5. **Everything is logged**. Every cgroup operation (create, destroy, limit change) is written to an audit log with the caller's host PID, the operation, and the result. This log is on the host filesystem, outside the container's reach.

6. **The shim dies with the container**. The cgroup daemon and port-forward processes are tied to the container's lifecycle. When the container exits, the host cleans up. There are no orphaned privileged processes.

---

## Future Work

- **seccomp profiles**: Restrict the container's syscall surface beyond what Docker/Podman defaults provide. This would further reduce what a compromised container can attempt.
- **AppArmor/SELinux policies**: Mandatory access control profiles tailored to YOLO Jail's specific mount and capability set.
- **Formal verification**: The cgroup daemon protocol is small enough (3 operations, ~300 lines) to be a candidate for lightweight formal verification or property-based testing.
- **Socket authentication**: While SO_PEERCRED is unforgeable, we could add a per-session nonce exchanged at container startup to bind the socket to a specific container instance.
- **Read-only workspace mode**: For review-only agent tasks, mount `/workspace` as read-only to eliminate the largest remaining write surface.
