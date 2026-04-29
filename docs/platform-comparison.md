# Platform Comparison: Linux vs macOS

YOLO Jail runs on both Linux and macOS. This document is a comprehensive
comparison of the two platforms — architecture, feature support, and
runtime differences.

## Architecture Overview

### Linux (native)

```
┌──────────────────────────────────────────────┐
│  Linux Host (x86_64 or aarch64)              │
│                                              │
│  cli.py ──► podman/docker ──► Linux kernel   │
│                                  │           │
│                ┌─────────────────▼────────┐  │
│                │  yolo-jail container      │  │
│                │  (same kernel, native)    │  │
│                │                          │  │
│                │  entrypoint.py           │  │
│                │  AI agent                │  │
│                │  /workspace (bind mount) │  │
│                └──────────────────────────┘  │
│                                              │
│  /sys/fs/cgroup ← cgroup delegation daemon   │
│  /dev/* ← device passthrough                 │
│  nvidia-smi ← GPU access                     │
└──────────────────────────────────────────────┘
```

On Linux, the container shares the **host kernel** directly. All kernel
features — cgroups, device nodes, user namespaces — are available natively.
There is zero virtualisation overhead.

### macOS — Docker / Podman (VM-mediated)

```
┌──────────────────────────────────────────────┐
│  macOS Host (Apple Silicon or Intel)         │
│                                              │
│  cli.py ──► podman/docker CLI                │
│                 │                            │
│  ┌──────────────▼───────────────────────┐    │
│  │  Linux VM (Podman Machine / Colima)  │    │
│  │  Apple Hypervisor / Virtualization.f │    │
│  │                                      │    │
│  │  ┌──────────────────────────────┐    │    │
│  │  │  yolo-jail container          │    │    │
│  │  │  (runs on VM's Linux kernel)  │    │    │
│  │  │                              │    │    │
│  │  │  entrypoint.py               │    │    │
│  │  │  AI agent                    │    │    │
│  │  │  /workspace (VirtioFS mount) │    │    │
│  │  └──────────────────────────────┘    │    │
│  │                                      │    │
│  │  VM kernel has cgroups, /dev/fuse,   │    │
│  │  iptables, etc. — but host doesn't   │    │
│  └──────────────────────────────────────┘    │
│                                              │
│  No /sys/fs/cgroup (XNU kernel)              │
│  No /dev/bus/usb (IOKit instead)             │
│  No NVIDIA GPU (Metal instead)               │
└──────────────────────────────────────────────┘
```

On macOS with Docker or Podman, a **Linux VM** sits between the host and
the container. The runtime manages this VM transparently.

### macOS — Apple Container (per-container VM)

```
┌──────────────────────────────────────────────┐
│  macOS Host (Apple Silicon)                  │
│                                              │
│  cli.py ──► container CLI                    │
│                 │                            │
│  ┌──────────────▼───────────────────────┐    │
│  │  Virtualization.framework             │    │
│  │  (one VM per container — no shared VM)│    │
│  │                                       │    │
│  │  ┌──────────────────────────────┐    │    │
│  │  │  yolo-jail container/VM       │    │    │
│  │  │  (Linux kernel per container) │    │    │
│  │  │  entrypoint.py               │    │    │
│  │  │  --cpus / --memory native    │    │    │
│  │  │  --publish-socket native     │    │    │
│  │  └──────────────────────────────┘    │    │
│  └───────────────────────────────────────┘    │
│                                              │
│  Each container = isolated VM                │
│  Native per-container resource limits        │
│  Native Unix socket forwarding               │
│  Max ~22 bind mounts (VZ.framework limit)    │
└──────────────────────────────────────────────┘
```

Apple Container creates a **dedicated VM for each container** using Apple's
Virtualization.framework directly. There is no shared daemon VM — each jail
gets its own isolated kernel with native resource limits.

**Key insight:** `entrypoint.py` runs inside the container on a Linux kernel
and needs **no macOS changes**. Only `cli.py` (host-side) is platform-aware.

## Feature Matrix

| Feature | Linux | macOS Docker/Podman | macOS Apple Container | Notes |
|---------|:-----:|:-------------------:|:---------------------:|-------|
| **Core** | | | | |
| Container isolation | ✅ | ✅ | ✅ | Read-only root, tmpfs |
| Workspace mount (`/workspace`) | ✅ | ✅ | ✅ | VirtioFS on macOS |
| Container reuse (`exec` into existing) | ✅ | ✅ | ✅ | |
| `yolo check` diagnostics | ✅ | ✅ | ✅ | Runtime-aware output |
| `yolo ps` / `yolo stop` / `yolo clean` | ✅ | ✅ | ✅ | |
| Per-project config (`yolo-jail.jsonc`) | ✅ | ✅ | ✅ | |
| User config (`~/.config/yolo-jail/`) | ✅ | ✅ | ✅ | |
| | | | | |
| **Container Runtimes** | | | | |
| Podman (rootless) | ✅ | ✅ | N/A | Podman Machine on macOS |
| Docker | ✅ | ✅ | N/A | Docker Desktop / Colima |
| Apple Container | N/A | N/A | ✅ | `YOLO_RUNTIME=container` |
| Podman-in-Podman | ✅ | ✅ | ✅ | AC: own kernel with /dev/fuse |
| Docker-in-Docker | ✅ | ✅ | ✅ | AC: own kernel with /dev/fuse |
| Selfhosting (nested yolo-jail) | ✅ | ✅ | ✅ | All backends support nesting |
| Runtime auto-detection | ✅ | ✅ | ✅ | macOS: Container > Podman > Docker |
| `YOLO_RUNTIME` env override | ✅ | ✅ | ✅ | |
| | | | | |
| **Networking** | | | | |
| Bridge mode (default) | ✅ | ✅ | ✅¹ | ¹Container gets own IP on 192.168.64.x |
| `--add-host` DNS overrides | ✅ | ✅ | ❌ | AC: no `--add-host`; loophole intercepts skipped with warning |
| Host networking (`--network host`) | ✅ | ✅ | ❌ | Not supported by Apple Container |
| Port publishing (`network.ports`) | ✅ | ✅ | ✅ | |
| Port forwarding (`forward_host_ports`) | ✅ | ✅ | ✅ | Native `--publish-socket` on AC |
| Unix socket forwarding | ✅ | ❌ | ✅ | VirtioFS rejects sockets; AC native |
| TCP gateway fallback | N/A | ✅ | N/A | Docker/Podman only |
| | | | | |
| **Image Building** | | | | |
| `nix build .#dockerImage` | ✅ | ✅¹ | ✅¹ | ¹Requires Linux builder |
| Native ARM image (aarch64) | ✅ | ✅ | ✅ | |
| Image format | Docker V2 | Docker V2 | OCI² | ²Auto-converted via skopeo/podman/docker |
| | | | | |
| **Agent Support** | | | | |
| Claude Code / Copilot / Gemini | ✅ | ✅ | ✅ | |
| MCP server presets | ✅ | ✅ | ✅ | |
| LSP servers | ✅ | ✅ | ✅ | |
| `mise` tool management | ✅ | ✅ | ✅ | |
| | | | | |
| **Security** | | | | |
| Read-only root filesystem | ✅ | ✅ | ✅ | |
| User namespace isolation | ✅ | ✅ | ✅³ | ³Each AC container is its own VM |
| Capability control | ✅ | ✅ | ❌ | No `--cap-add` in AC |
| `--security-opt` | ✅ | ✅ | ❌ | Not supported by AC |
| Private cgroup namespace | ✅ | ✅ | ❌ | No `--cgroupns` in AC |
| | | | | |
| **Resource Limits** | | | | |
| Cgroup delegation daemon | ✅ | ❌ | ❌ | macOS has no host cgroups |
| In-container cgroups v2 (writable) | ✅ | ❌ | ✅ | AC: own kernel = full cgroup tree |
| `yolo-cglimit` in-container | ✅ | ❌ | ✅¹ | ¹Per-job CPU/mem limits work in AC! |
| Per-container CPU/memory | ✅ | ❌ | ✅ | AC: `--cpus`, `--memory` native |
| VM-level resource caps | N/A | ✅ | N/A | Podman Machine / Docker Desktop |
| | | | | |
| **Device & GPU Passthrough** | | | | |
| Raw `/dev/*` paths | ✅ | ❌ | ❌ | |
| USB device passthrough | ✅ | ❌ | ❌ | |
| Device cgroup rules | ✅ | ❌ | ❌ | |
| NVIDIA GPU (`--gpus`) | ✅ | ❌ | ❌ | |
| | | | | |
| **Filesystem** | | | | |
| Bind mounts | ✅ | ✅ | ⚠️⁴ | ⁴Max ~22 per container (VZ limit) |
| tmpfs mounts | ✅ | ✅ | ✅ | No options syntax on AC |
| `/dev/fuse` passthrough | ✅ | ✅ | ❌ | |
| Nix store mount (`/nix`) | ✅ | ⚠️ | ❌ | Skipped on AC |

**Legend:** ✅ = fully supported, ⚠️ = partially supported / needs config,
❌ = not available (gracefully skipped with warning), N/A = not applicable

## How Platform Detection Works

`cli.py` sets two constants at module load:

```python
IS_LINUX = sys.platform == "linux"
IS_MACOS = sys.platform == "darwin"
```

These gate all platform-specific behaviour:

| Code Location | What It Guards |
|---|---|
| `start_cgroup_delegate()` | Skips daemon startup; creates empty socket dir |
| `_resolve_container_cgroup()` | Returns `None` (no `/proc` on macOS host) |
| Container detection (`in_container`) | macOS host is never inside a container |
| Device passthrough loop | Warns and skips each device entry |
| GPU passthrough block | Warns and skips `--gpus` / CDI flags |
| `check()` GPU section | Reports GPU unavailable instead of probing `nvidia-smi` |

`entrypoint.py` has **no platform guards** — it always runs on Linux inside
the container.

## Nix Cross-Building

The `flake.nix` uses a mapping to build Linux packages from macOS:

```nix
# Maps aarch64-darwin → aarch64-linux, x86_64-darwin → x86_64-linux
imageSystem = builtins.replaceStrings ["-darwin"] ["-linux"] system;
imagePkgs   = nixpkgs.legacyPackages.${imageSystem};
```

All Docker image content uses `imagePkgs` (Linux packages). The devShell
uses `pkgs` (host-native packages, macOS on Mac).

| Variable | Linux Host | macOS Host |
|---|---|---|
| `system` | `x86_64-linux` or `aarch64-linux` | `x86_64-darwin` or `aarch64-darwin` |
| `imageSystem` | same as `system` | `x86_64-linux` or `aarch64-linux` |
| `imagePkgs` | same as `pkgs` | Linux packages from binary cache |
| `pkgs` | Linux packages | macOS packages (devShell only) |

Standard nixpkgs packages are pre-built in the NixOS binary cache, so no
local compilation is needed — Nix downloads the aarch64-linux binaries.

## Performance Differences

| Aspect | Linux | macOS |
|---|---|---|
| Container startup | ~1s | ~2-3s (VM overhead) |
| File I/O (`/workspace`) | Native bind mount | VirtioFS (near-native) |
| Network I/O | Native bridge/host | VM NAT (negligible overhead) |
| CPU-bound workloads | Native | ~95-98% native (thin VM) |
| First `nix build` | ~2-5 min (download) | ~5-10 min (download + evaluation) |
| Subsequent `nix build` | Instant (cached) | Instant (cached) |

VirtioFS on Apple Silicon provides near-native file performance. The VM
layer adds minimal overhead for compute-bound AI agent tasks.

## Container Runtime Comparison (macOS)

| | Podman Machine | Docker Desktop | Colima | Apple Container |
|---|---|---|---|---|
| VM technology | Apple HV (applehv) | Apple HV | Virtualization.f (vz) | Virtualization.f (per-container) |
| License | Free / open source | Free (personal) / paid | Free / open source | Free / open source (Apple) |
| Podman-in-Podman | ✅ Native | ❌ | ❌ | ✅ (own kernel + /dev/fuse) |
| Docker-in-Docker | ❌ | ✅ Native | ✅ (daemon in VM) | ✅ (own kernel + /dev/fuse) |
| In-container cgroups | ❌ (read-only) | ❌ (read-only) | ❌ (read-only) | ✅ (writable, own kernel) |
| Per-container CPU/mem | ❌ (VM-level only) | ❌ (VM-level only) | ❌ (VM-level only) | ✅ (`--cpus`, `--memory`) |
| Resource defaults | Set at VM init | Set in GUI | Set at VM start | Auto: ½ host CPUs + ½ RAM |
| File sharing | VirtioFS | VirtioFS / gRPC FUSE | VirtioFS (vz) | VirtioFS (per-container) |
| Unix socket fwd | ❌ | ❌ | ❌ | ✅ (`--publish-socket`) |
| Max bind mounts | Unlimited | Unlimited | Unlimited | ~22 (VZ.framework limit) |
| Image format | Docker V2 | Docker V2 | Docker V2 | OCI layout |
| `YOLO_RUNTIME=` | `podman` | `docker` | `docker` | `container` |
| Maturity | Mature | Mature | Mature | Early stage (v0.x) |

## Error Handling on macOS

When a Linux-only feature is used on macOS, `cli.py` prints a yellow
warning and **continues** — it never fails:

```
Warning: device passthrough (/dev/bus/usb/001/004) not supported on macOS — skipping
Warning: USB device passthrough (RTL-SDR) not supported on macOS — skipping
Warning: device cgroup rules not supported on macOS — skipping
Warning: GPU passthrough is not supported on macOS — skipping
```

The `yolo check` command also reports macOS limitations:

```
GPU (NVIDIA)
  ⚠ GPU passthrough is not supported on macOS
    NVIDIA GPU passthrough requires Linux with NVIDIA drivers
```

## Test Suite on macOS

All unit tests run on macOS via a `conftest.py` autouse fixture that patches
`IS_MACOS=False` / `IS_LINUX=True` so tests exercise the primary Linux code
paths. This ensures:

- Tests don't diverge based on developer's OS
- Linux-specific assertions still pass
- Tests that need real macOS behaviour can override via `monkeypatch`

`tests/test_macos_paths.py` contains dedicated tests that explicitly set
`IS_MACOS=True` and exercise every macOS code path with mocked Docker/Nix.
This includes 15 Docker/Podman macOS tests and 10 Apple Container-specific
tests covering runtime detection, image commands, container listing, stuck
check handling, flag filtering, and `_runtime_for_check()`.

Integration tests (`test_jail.py`) that need cgroup access auto-skip via
`_skip_if_cgroup_readonly()` since `/sys/fs/cgroup` doesn't exist on macOS.

```
$ pytest tests/ -m "not slow"
======================== 486 passed, 1 skipped ========================
```

The 1 skip is `TestCgroupDaemonSocket::test_start_stop_lifecycle` — the
daemon can't start without real cgroup v2.
