# YOLO Jail User Guide

This guide covers everything you need to get started with YOLO Jail and make the most of its features. For quick-start instructions, see the [README](../README.md).

**YOLO Jail runs on Linux and macOS as first-class platforms.** Every section below shows instructions for both where they differ. Linux uses Docker or Podman; macOS uses Docker (via Docker Desktop or Colima), Podman Machine, or Apple Container. For the full macOS-specific setup, see [docs/macos.md](macos.md); for a feature-by-feature comparison, see [docs/platform-comparison.md](platform-comparison.md).

---

## Table of Contents

- [Installation](#installation)
- [First Run](#first-run)
- [Authentication](#authentication)
- [CLI Commands](#cli-commands)
- [Configuration](#configuration)
- [Network & Ports](#network--ports)
- [MCP Presets](#mcp-presets)
- [LSP Servers](#lsp-servers)
- [Package Management](#package-management)
- [Blocked Tools](#blocked-tools)
- [Device Passthrough](#device-passthrough)
- [GPU Passthrough (NVIDIA)](#gpu-passthrough-nvidia)
- [Host Services](#host-services)
- [Storage & Persistence](#storage--persistence)
- [Container Reuse](#container-reuse)
- [Config Safety](#config-safety)
- [Platform Differences Reference](#platform-differences-reference)
- [Troubleshooting](#troubleshooting)

---

## Installation

### Prerequisites (both platforms)

| Tool | Purpose | Install |
|------|---------|---------|
| [uv](https://docs.astral.sh/uv/) | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Nix](https://nixos.org/download/) | Image builder (with flakes) | [Determinate Nix Installer](https://github.com/DeterminateSystems/nix-installer) recommended |
| [just](https://github.com/casey/just) | Task runner for `just deploy` | `cargo install just`, `brew install just`, or your package manager |
| Container runtime | Docker, Podman, or Apple Container | See platform-specific setup below |

**Supported platforms:** Linux (x86_64, aarch64) and macOS (Apple Silicon and Intel). On macOS, containers run in a lightweight Linux VM managed by Docker Desktop, Colima, Podman Machine, or Apple Container.

### Container Runtime Setup

Pick the runtime that fits your platform. YOLO Jail auto-detects whichever is available; the env var `YOLO_RUNTIME` (or the `runtime` key in config) forces a specific one.

#### Linux

```bash
# Podman (preferred — rootless by default, no daemon)
sudo apt-get install podman          # Debian/Ubuntu
sudo dnf install podman              # Fedora/RHEL
sudo pacman -S podman                # Arch

# Docker
sudo apt-get install docker.io       # Debian/Ubuntu
sudo usermod -aG docker $USER
# (log out and back in for group membership to take effect)
```

Auto-detect priority on Linux: **podman → docker**.

#### macOS

You have three runtime choices. Pick one based on your needs:

**Option A — Apple Container (native, recommended for desktop Macs on macOS 15+):**

```bash
brew install container skopeo
container system start
```

Native per-container CPU/memory limits, native Unix socket port forwarding, smallest footprint (no separate VM daemon). Has a ~22 bind mount limit — YOLO Jail works around this by consolidating workspace state into one mount. `skopeo` is used to convert Nix's Docker V2 image tarballs to OCI for Apple Container.

**Option B — Docker via Colima (recommended for headless/CI Macs):**

```bash
brew install colima docker
colima start --cpu 4 --memory 8 --disk 30 \
  --mount-type virtiofs \
  --mount "$HOME:w" \
  --mount /private/var/folders:w \
  --mount /private/tmp:w
```

Works without a GUI session (unlike Docker Desktop / Podman Machine). Uses VZ.framework under the hood on modern Macs.

**Option C — Podman Machine:**

```bash
brew install podman
podman machine init --cpus 4 --memory 8192 --disk-size 50
podman machine start
```

Good if you already use Podman on Linux and want the same CLI on macOS. Requires a GUI session on some versions.

Auto-detect priority on macOS: **container → podman → docker**.

#### Nix remote Linux builder (macOS only)

The container image is a Linux image. Nix needs a remote Linux builder to compile or fetch `aarch64-linux`/`x86_64-linux` packages. See [docs/macos.md § Nix Linux Builder](macos.md#nix-linux-builder-for-building-the-image-from-source) for the full setup — in short, install Nix inside Colima or Podman Machine and register it as a builder in `/etc/nix/machines`.

### Install YOLO Jail

Two install paths, pick whichever fits:

#### Option A — Homebrew (easiest, both macOS and Linux)

```bash
brew tap mschulkind-oss/tap
brew install mschulkind-oss/tap/yolo-jail
```

| Pros | Cons |
|---|---|
| Single command | No refresher auto-install |
| Auto-upgrades via `brew upgrade` | No source checkout available for hacking |
| No `just`, no source, no build tools | |
| Works on macOS and Linuxbrew identically | |

This is the recommended path for users who just want to run yolo-jail. The Homebrew formula is published to [mschulkind-oss/homebrew-tap](https://github.com/mschulkind-oss/homebrew-tap) automatically on every release via the `brew` job in `.github/workflows/publish.yml`.

**Note:** the Homebrew install does **not** set up the host-side Claude OAuth token refresher. If you run many jails in parallel against one Claude account and want to avoid refresh-token races, either install from source (Option B) to get the systemd timer, or follow [scripts/README.md](../scripts/README.md) to install the refresher manually via launchd (macOS) or systemd user units (Linux).

#### Option B — Install from source

Required if you want the token refresher auto-installed via `just deploy`, or if you're hacking on yolo-jail. Identical on Linux and macOS:

```bash
git clone https://github.com/mschulkind-oss/yolo-jail.git
cd yolo-jail
just deploy      # builds + installs yolo CLI + host-side token refresher
```

`just deploy` is idempotent and safe to re-run. On Linux it installs a systemd `--user` timer for the Claude OAuth token refresher. On macOS (no systemd `--user`), the same script is installable via cron or launchd — see [scripts/README.md](../scripts/README.md) for launchd instructions.

To upgrade later:

```bash
cd yolo-jail && git pull && just deploy
```

### Set Up User Defaults (Optional)

```bash
yolo init-user-config
# Edit: ~/.config/yolo-jail/config.jsonc
```

Same path and merge semantics on Linux and macOS. User-level defaults apply to all projects and are merged under workspace config.

---

## First Run

Navigate to any repository and run:

```bash
cd ~/code/my-project
yolo
```

On first run, YOLO Jail will:

1. **Build the Linux container image** via `nix build`:
   - **Linux:** Nix downloads prebuilt packages from the binary cache (~2–5 minutes).
   - **macOS:** Nix dispatches the image build to the remote Linux builder you configured (~5–10 minutes the first time, instant on subsequent runs thanks to caching).
2. **Load the image** into your container runtime:
   - Docker / Podman: `docker load` / `podman load` from the cached tarball
   - Apple Container: the tarball is converted from Docker V2 to OCI via `skopeo` (or `podman`/`docker` as fallback) and then `container image load`ed
3. **Install tools** — MCP servers, LSP servers, and utilities are installed into persistent storage (`~/.local/share/yolo-jail/home/`).
4. **Start your command** — by default, an interactive shell.

Subsequent runs skip steps 1–3 (everything is cached) and start in seconds on both platforms.

---

## Authentication

Inside the jail, authenticate with your tools once:

```bash
gh auth login          # GitHub CLI
gemini login           # Google Gemini CLI
claude                 # Runs /login on first launch
```

Tokens are stored in `~/.local/share/yolo-jail/home/` on the host (same path on Linux and macOS) and persist across jail restarts. You do **not** need to re-authenticate each time, and on Docker/Podman runtimes a `/login` in any jail propagates to every other jail automatically.

### Claude OAuth token refresher

Anthropic uses single-use refresh tokens — when multiple jails share the same `.credentials.json` and two of them try to refresh the OAuth token in the same window, one loses the race and gets logged out. YOLO Jail ships a **host-side refresher** that keeps the shared token fresh on a timer, so jails never refresh on their own.

`just deploy` installs the refresher. On Linux, it runs as a systemd `--user` timer (every 10 minutes, refreshes when the token has under 30 minutes of headroom). On macOS, install via launchd or cron — see [scripts/README.md](../scripts/README.md).

Once installed, `yolo doctor` (alias for `yolo check`) includes a "Claude Token Refresher" section that reports:

- Script presence + executable
- Credentials parse + remaining headroom
- (Linux) systemd unit installed, timer enabled + active, last run state, next scheduled tick

> **Security note:** Auth tokens are stored separately from your host credentials. The jail never accesses your host `~/.ssh/`, `~/.gitconfig`, or cloud credentials. The token refresher reads only `~/.local/share/yolo-jail/home/.claude/.credentials.json` — never your host `~/.claude/`.

---

## CLI Commands

### `yolo` — Start a Jail

```bash
yolo                       # Interactive shell
yolo -- claude             # Start Claude Code in YOLO mode
yolo -- copilot            # Start Copilot (--yolo auto-injected)
yolo -- gemini             # Start Gemini (--yolo auto-injected)
yolo -- bash -c "make"     # Run a specific command
```

**Options:**
- `--new` — Force a new container even if one exists for this workspace
- `--network bridge|host` — Override network mode for this run
- `--profile` — Show detailed startup performance timing

### `yolo init` — Initialize a Project

```bash
cd ~/code/my-project
yolo init
```

Creates a `yolo-jail.jsonc` config with documented defaults and adds `.yolo/` to `.gitignore`.

### `yolo check` — Validate Everything

```bash
yolo check              # Full check including nix build
yolo check --no-build   # Quick check (skip nix build)
```

**Run this after every edit to `yolo-jail.jsonc`.** It validates:
- Container runtime availability
- Nix installation and flakes support
- Config file syntax and schema
- Entrypoint dry-run (shims, MCP, LSP generation)
- Nix image build (unless `--no-build`)

Inside a running jail, use `yolo check --no-build` for a fast preflight before asking for a restart.

### `yolo doctor` — Alias for Check

```bash
yolo doctor             # Same as yolo check
```

### `yolo ps` — List Running Jails

```bash
yolo ps
```

Shows container names, status, uptime, and workspace mappings.

### `yolo config-ref` — Full Configuration Reference

```bash
yolo config-ref
```

Prints the complete reference for all `yolo-jail.jsonc` fields with types, defaults, and examples.

### `yolo init-user-config` — Create User Defaults

```bash
yolo init-user-config
```

Creates `~/.config/yolo-jail/config.jsonc` with the same template as `yolo init`.

---

## Configuration

YOLO Jail is configured via JSONC (JSON with comments) files:

| File | Scope | Purpose |
|------|-------|---------|
| `yolo-jail.jsonc` | Workspace | Per-project settings |
| `~/.config/yolo-jail/config.jsonc` | User | Global defaults for all projects |

**Merge rules:** Workspace config merges over user defaults. Lists are merged and deduplicated; scalars and objects in workspace override user values.

### Minimal Example

```jsonc
{
  "runtime": "podman",
  "packages": ["postgresql", "redis"],
  "mcp_presets": ["chrome-devtools"]
}
```

### Full Example

```jsonc
{
  // Container runtime: "podman", "docker", or "container" (Apple Container)
  "runtime": "podman",

  // Extra nix packages baked into the image
  "packages": ["postgresql", "htop", "strace"],

  // Network configuration
  "network": {
    "mode": "bridge",
    "ports": ["8000:8000", "3000:3000"],
    "forward_host_ports": [5432, 6379]
  },

  // Security settings
  "security": {
    "blocked_tools": [
      {"name": "grep", "message": "Use rg", "suggestion": "rg <pattern>"},
      {"name": "find", "message": "Use fd"},
      "curl"
    ]
  },

  // Extra read-only mounts
  "mounts": ["~/code/shared-lib"],

  // MCP presets (opt-in)
  "mcp_presets": ["chrome-devtools", "sequential-thinking"],

  // Custom MCP servers
  "mcp_servers": {
    "my-custom": {
      "command": "/workspace/scripts/my-mcp-server.py",
      "args": []
    }
  },

  // Extra tools via mise
  "mise_tools": {"neovim": "stable", "typst": "latest"},

  // Additional LSP servers
  "lsp_servers": {
    "rust": {
      "command": "rust-analyzer",
      "args": [],
      "fileExtensions": {".rs": "rust"}
    }
  }
}
```

Run `yolo config-ref` for the complete field reference.

---

## Network & Ports

### Bridge Mode (Default)

The jail runs in an isolated network. Use `ports` to publish container services to the host:

```jsonc
{
  "network": {
    "mode": "bridge",
    "ports": ["8000:8000"]
  }
}
```

A service running on port 8000 inside the jail is accessible at `localhost:8000` on the host.

### Host Mode

Share the host's network stack directly:

```jsonc
{
  "network": {
    "mode": "host"
  }
}
```

All ports work as if running on the host. No port mapping needed.

### Host Port Forwarding

Make host services appear on `localhost` inside the jail — useful for databases, APIs, or other services already running on your machine:

```jsonc
{
  "network": {
    "forward_host_ports": [5432, 6379, "8080:9090"]
  }
}
```

- **Integer** (`5432`): Same port on both sides — host `127.0.0.1:5432` appears as jail `127.0.0.1:5432`
- **String** (`"8080:9090"`): Port remapping — host `127.0.0.1:9090` appears as jail `127.0.0.1:8080`

This uses socat via Unix sockets (requires `socat` on the host). Only works in bridge mode.

---

## MCP Presets

MCP (Model Context Protocol) servers extend agent capabilities. YOLO Jail includes built-in presets that can be enabled by name — **none are enabled by default**.

### Available Presets

| Preset | Description |
|--------|-------------|
| `chrome-devtools` | Headless Chromium automation via Chrome DevTools Protocol |
| `sequential-thinking` | Chain-of-thought reasoning MCP server |

### Enable Presets

```jsonc
{
  "mcp_presets": ["chrome-devtools", "sequential-thinking"]
}
```

### Custom MCP Servers

Add your own MCP servers alongside or instead of presets:

```jsonc
{
  "mcp_servers": {
    "my-server": {
      "command": "/workspace/scripts/my-mcp.py",
      "args": ["--port", "3333"]
    }
  }
}
```

### Disable a Preset

Set a preset server to `null` in `mcp_servers` to disable it even when listed in `mcp_presets`:

```jsonc
{
  "mcp_presets": ["chrome-devtools", "sequential-thinking"],
  "mcp_servers": {
    "sequential-thinking": null
  }
}
```

---

## LSP Servers

YOLO Jail configures LSP (Language Server Protocol) servers for Claude Code, Copilot, and Gemini. Three servers are always available:

| Language | Server | Extensions |
|----------|--------|------------|
| Python | Pyright | `.py`, `.pyi` |
| TypeScript/JavaScript | typescript-language-server | `.ts`, `.tsx`, `.js`, `.jsx` |
| Go | gopls | `.go` |

### Adding Servers

Add language servers via `lsp_servers` in your config. The binary must be on PATH (install via `mise_tools` or `packages`):

```jsonc
{
  "lsp_servers": {
    "rust": {
      "command": "rust-analyzer",
      "args": [],
      "fileExtensions": {".rs": "rust"}
    }
  }
}
```

Workspace servers are merged with defaults — you can add new ones or override existing ones.

### How It Works

- **Claude Code** receives LSP servers via plugins or MCP
- **Copilot** receives native LSP config via `~/.copilot/lsp-config.json`
- **Gemini** receives LSP servers wrapped as MCP servers via `mcp-language-server`
- Servers are spawned on-demand when agents analyze matching file types

---

## Package Management

### Nix Packages (Image-Level)

Add system packages via the `packages` config array. These are baked into the container image:

```jsonc
{
  "packages": ["postgresql", "strace", "htop"]
}
```

Package names must match [nixpkgs attributes](https://search.nixos.org/packages). The image only rebuilds when this list changes.

**Pinned versions:** Pin to a specific nixpkgs commit for reproducibility:

```jsonc
{
  "packages": [
    "postgresql",
    {"name": "freetype", "nixpkgs": "e6f23dc0..."}
  ]
}
```

Find nixpkgs commits for specific versions at [lazamar.co.uk/nix-versions](https://lazamar.co.uk/nix-versions/).

### Mise Tools (Runtime-Level)

Add tools to your workspace's `mise.toml` for workspace-specific runtimes:

```toml
# mise.toml
[tools]
typst = "latest"
rust = "1.80"
```

On jail startup, `mise install` fetches declared tools. They persist across restarts in `~/.local/share/mise/`.

To inject tools into all jails globally, use `mise_tools` in your config:

```jsonc
{
  "mise_tools": {"neovim": "stable", "typst": "latest"}
}
```

---

## Blocked Tools

YOLO Jail blocks certain tools by default and suggests faster alternatives:

| Blocked | Suggestion |
|---------|-----------|
| `grep` | Use `rg` (ripgrep) |
| `find` | Use `fd` |
| `apt` / `apt-get` | Use `packages` in `yolo-jail.jsonc` |
| `pip` | Use `uv` |

### Customize Blocked Tools

```jsonc
{
  "security": {
    "blocked_tools": [
      {"name": "grep", "message": "Use rg", "suggestion": "rg <pattern>"},
      {"name": "curl", "message": "Network access blocked"}
    ]
  }
}
```

### Bypass

Set `YOLO_BYPASS_SHIMS=1` in scripts that need blocked tools:

```bash
YOLO_BYPASS_SHIMS=1 grep -r "pattern" .
```

---

## Device Passthrough

**Platform support:** Device passthrough (USB, serial, cgroup rules) is a **Linux-only** feature. It relies on the host kernel exposing `/dev/bus/usb/`, `/dev/tty*`, and `--device-cgroup-rule` — none of which exist on macOS where containers run inside a VM. On macOS, device entries in `yolo-jail.jsonc` are parsed, logged as skipped with a warning, and do not prevent the jail from starting.

On Linux, pass host devices (USB, serial, etc.) into the jail:

```jsonc
{
  "devices": [
    {"usb": "0bda:2838", "description": "RTL-SDR"},
    "/dev/ttyUSB0",
    {"cgroup_rule": "c 189:* rwm"}
  ]
}
```

**Formats:**
- **USB by vendor:product ID** (preferred — stable across reboots): `{"usb": "0bda:2838"}`
- **Raw device path** (changes on replug): `"/dev/bus/usb/001/004"`
- **Cgroup rule** (broad access): `{"cgroup_rule": "c 189:* rwm"}`

Missing devices produce a warning but don't prevent the jail from starting. Device changes are subject to [config safety](#config-safety) approval.

---

## GPU Passthrough (NVIDIA)

**Platform support:** GPU passthrough is **Linux-only**. Apple Silicon Macs use Metal, not CUDA/OpenCL, and Apple's Virtualization.framework doesn't expose the GPU to the guest Linux kernel. If `"gpu": {"enabled": true}` appears in `yolo-jail.jsonc` on macOS, it is parsed, logged as skipped with a warning, and does not prevent the jail from starting. For GPU workflows on macOS, run on a Linux box (local or EC2 `g5`/`p3` instance) instead.

On Linux, train deep learning models inside the jail using NVIDIA GPUs. Requires the NVIDIA Container Toolkit on the host.

### Host Setup

1. **Verify your GPU driver:**
   ```bash
   nvidia-smi
   ```

2. **Install the NVIDIA Container Toolkit:**
   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
     | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
     | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   ```

3. **Configure the container runtime:**
   ```bash
   # Docker
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker

   # Podman (CDI)
   sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
   ```

4. **Validate:**
   ```bash
   yolo check   # GPU section will show nvidia-smi, nvidia-ctk, CDI spec status
   ```

### Jail Configuration

```jsonc
// yolo-jail.jsonc
{
  "gpu": {
    "enabled": true,
    "devices": "all",
    "capabilities": "compute,utility"
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable GPU passthrough |
| `devices` | `"all"` | `"all"`, or specific GPUs: `"0"`, `"0,1"`, `"GPU-<uuid>"` |
| `capabilities` | `"compute,utility"` | NVIDIA driver capabilities to expose |

### Installing PyTorch

Once inside the GPU-enabled jail:

```bash
pip install torch torchvision
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### Runtime Details

| Runtime | Mechanism | Notes |
|---------|-----------|-------|
| **Docker** | `--gpus all` | Requires `nvidia-ctk runtime configure --runtime=docker` |
| **Podman** | `--device nvidia.com/gpu=all` (CDI) | Requires CDI spec at `/etc/cdi/nvidia.yaml` |

- **Podman rootless:** GPU passthrough uses `--userns=keep-id` and `--runtime=runc` (crun has CDI bugs). Nested podman-in-podman is not available when GPU is active.
- **Shared memory:** The jail uses `--shm-size=2g` for PyTorch multi-process data loading.
- **CUDA forward compatibility:** CUDA in the container can be newer than the host driver, but not the reverse.

### AWS EC2

Use an AWS Deep Learning AMI (DLAMI) — drivers and toolkit come pre-installed.

| Instance | GPU | VRAM | Use Case | $/hr (approx) |
|----------|-----|------|----------|----------------|
| g4dn.xlarge | 1× T4 | 16 GB | Inference, light training | ~$0.53 |
| g5.xlarge | 1× A10G | 24 GB | Training + inference | ~$1.01 |
| p3.2xlarge | 1× V100 | 16 GB | Training | ~$3.06 |

### Troubleshooting GPU

- **`conmon bytes "": readObjectStart` error (Podman):** Caused by `crun` OCI runtime's CDI handling bug ([podman#27483](https://github.com/containers/podman/issues/27483)). The jail automatically uses `--runtime runc` when GPU is enabled to work around this. If you see this, update your `yolo-jail` installation.
- **`nvidia-smi` not found inside jail:** The NVIDIA Container Toolkit injects driver libs at container start. Check the toolkit is installed and configured on the host.
- **CUDA out of memory:** Reduce batch size, or limit which GPUs are exposed with `"devices": "0"`.

---

## Host Services

**A way to split the jail boundary cleanly.** A host service is a process that runs on the host (outside the jail) and exposes a Unix socket that gets bind-mounted into the jail at `/run/yolo-services/<name>.sock`. The agent inside the jail can talk to the service without ever holding the service's secrets, credentials, or privileges.

This is exactly the pattern used by the built-in cgroup delegate daemon: a host-side process performs privileged cgroup operations on behalf of the container so the jail itself doesn't need `CAP_SYS_ADMIN` or rw cgroup mounts. `host_services` lets you define your own services that follow the same pattern.

### When to use it

- **Auth / credential brokers.** A service holds API keys, OAuth tokens, or signed JWTs and answers scoped requests from the agent. The jail never sees the raw credentials.
- **Access control proxies.** A service fronts an internal API and enforces "agent X may only call endpoint Y with payload Z" rules outside the jail.
- **Audit / logging sinks.** A service receives structured events from the agent and writes them to a host-side log the jail can't tamper with.
- **Resource brokers.** Anything where you want a small piece of host-side trust without pulling the entire dependency into the jail.

### Configuration

```jsonc
{
  "host_services": {
    "auth-broker": {
      // Command to launch on the host when the jail starts.
      // "{socket}" is substituted with the host-side socket path the
      // service should bind.
      "command": ["~/code/auth-broker/serve.py", "--socket", "{socket}"],

      // Optional environment variables for the host daemon (NOT the jail).
      "env": {
        "KEYS_FILE": "~/secrets/broker-keys.json",
        "LOG_LEVEL": "info"
      },

      // Optional override of where the socket appears inside the jail.
      // Must start with /run/yolo-services/ — that's the only directory
      // that gets bind-mounted in.  Default: /run/yolo-services/<name>.sock
      "jail_socket": "/run/yolo-services/auth-broker.sock"
    }
  }
}
```

The service name (`auth-broker` above) must match `^[a-zA-Z][a-zA-Z0-9_-]{0,63}$`. The name `cgroup-delegate` is reserved for the built-in.

### Lifecycle

For each service, on `yolo run`:

1. Per-jail directory `<workspace>/.yolo/host-services/` is created on the host and bind-mounted into the jail at `/run/yolo-services/`.
2. yolo substitutes `{socket}` in the service's command with the host-side path, e.g. `<workspace>/.yolo/host-services/auth-broker.sock`.
3. yolo launches the command as a child process. The service is expected to bind the socket at the substituted path.
4. yolo waits up to 5 seconds for the socket file to appear. If the service exits early or doesn't bind in time, yolo logs the failure and continues without that service.
5. The container starts. The agent inside sees `/run/yolo-services/auth-broker.sock` and can connect.
6. When the container exits, yolo sends `SIGTERM` to each service, waits 5 seconds, then `SIGKILL`.
7. The per-jail sockets directory is removed.

Service stdout and stderr are captured to `~/.local/share/yolo-jail/logs/host-service-<name>.log` for debugging.

### Discovering the socket from inside the jail

For each service, yolo injects an env var so the agent doesn't need to hard-code the path:

```
YOLO_SERVICE_AUTH_BROKER_SOCKET=/run/yolo-services/auth-broker.sock
```

The variable name is `YOLO_SERVICE_<UPPERCASED-NAME>_SOCKET`, with non-alphanumeric characters replaced by underscores.

### Minimal example service

A trivial Python broker that hands out a single secret. The service runs on the host, holds the secret, and never reveals it to the jail — the jail just gets the resolved value for the key it asks about.

```python
# ~/code/auth-broker/serve.py
import json, os, socket, sys

KEYS = json.load(open(os.environ["KEYS_FILE"]))

sock_path = sys.argv[sys.argv.index("--socket") + 1]
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(8)

while True:
    conn, _ = srv.accept()
    try:
        line = b""
        while not line.endswith(b"\n"):
            chunk = conn.recv(4096)
            if not chunk:
                break
            line += chunk
        req = json.loads(line)
        # Toy access control: the agent can only ask for keys in an allowlist.
        key = req.get("key")
        if key in {"OPENAI_API_KEY", "STRIPE_SECRET"}:
            conn.sendall(json.dumps({"value": KEYS[key]}).encode() + b"\n")
        else:
            conn.sendall(json.dumps({"error": "key not allowed"}).encode() + b"\n")
    finally:
        conn.close()
```

Hook it up in your workspace config:

```jsonc
{
  "host_services": {
    "auth-broker": {
      "command": ["python3", "~/code/auth-broker/serve.py", "--socket", "{socket}"],
      "env": {"KEYS_FILE": "~/secrets/keys.json"}
    }
  }
}
```

Inside the jail, the agent uses `$YOLO_SERVICE_AUTH_BROKER_SOCKET`:

```bash
echo '{"key": "OPENAI_API_KEY"}' | nc -U "$YOLO_SERVICE_AUTH_BROKER_SOCKET"
# {"value": "sk-..."}
```

The secret never enters the jail filesystem, env vars, or any bind mount.

### Security model

- Each service's socket lives in a per-jail directory bind-mounted to `/run/yolo-services/`. Other jails can't see it.
- On Linux, services can use `SO_PEERCRED` on accepted connections to attest the caller's host PID — same mechanism the cgroup delegate uses.
- What the service does with secrets, scopes, audit logging, and rate limiting is entirely up to the service. yolo just wires the plumbing.
- The cgroup delegate daemon is one of these services internally — proof that the pattern is enough to support privileged operations safely.

### Validation

`yolo check` verifies that each configured service's command exists and is executable. Catches typos before the next jail start.

### Apple Container caveat

Apple Container doesn't bind-mount Unix sockets through virtiofs, so host services are skipped entirely on the `container` runtime. Use `podman` or `docker` if you need this feature on macOS.

---

## Storage & Persistence

All paths below use the same layout on Linux and macOS (`~/.local/share/yolo-jail/` resolves to `/home/$USER/.local/share/yolo-jail/` on Linux and `/Users/$USER/.local/share/yolo-jail/` on macOS). On macOS with Docker Desktop or Colima, make sure `$HOME` is in the Colima/Docker shared folders list (the Colima `--mount "$HOME:w"` flag, Docker Desktop's Resources → File Sharing).

### Timezone

The host's timezone is passed into the jail via the `TZ` env var, so `date`, log timestamps, cron expressions, and file mtimes inside the jail report the same wall-clock time as the host. Detection order:

1. `$TZ` on the host (if you've explicitly set one, it wins)
2. `/etc/timezone` plain-text zone name (Debian, Ubuntu, Arch)
3. `/etc/localtime` symlink target suffix (Fedora, macOS — `/var/db/timezone/zoneinfo/<zone>`)

If none of these resolve, the jail falls back to UTC. Override per-jail by exporting `TZ` in the shell you use to launch `yolo`, or by setting it in `env` inside `yolo-jail.jsonc`.

### What Persists Across Restarts

| Data | Location (Host) | Shared? |
|------|-----------------|---------|
| Auth tokens (gh, gemini, claude) | `~/.local/share/yolo-jail/home/` | All jails |
| Installed tools (npm, go) | `~/.local/share/yolo-jail/home/` | All jails |
| Mise tools & runtimes | `~/.local/share/mise/` (Linux); Docker named volume `yolo-mise-data` (macOS) | All jails |
| Bash history | `<workspace>/.yolo/home/bash_history` | Per workspace |
| Claude sessions | `<workspace>/.yolo/home/claude-projects/` | Per workspace |
| Copilot sessions | `<workspace>/.yolo/home/copilot-sessions/` | Per workspace |
| Gemini history | `<workspace>/.yolo/home/gemini-history/` | Per workspace |
| SSH keys | `<workspace>/.yolo/home/ssh/` | Per workspace |

**Why mise differs on macOS:** The host `~/.local/share/mise/` on macOS contains Mach-O (darwin) binaries that cannot execute inside the Linux container. Instead of bind-mounting the host directory, YOLO Jail uses a Docker named volume (`yolo-mise-data`) so the container installs its own native Linux toolchains. The volume persists across jail restarts.

### What Gets Regenerated

On every jail start, the entrypoint regenerates:
- `.bashrc` — prompt, aliases, PATH, mise integration
- Shim scripts — blocked tool interceptors
- MCP config — `mcp-config.json` / `settings.json`
- LSP config — `lsp-config.json`
- Bootstrap script — tool installation (idempotent)

---

## Container Reuse

By default, `yolo` reuses an existing container for the same workspace:

```bash
yolo             # Creates container yolo-<hash>
yolo             # Reuses yolo-<hash> via exec
yolo --new       # Forces a new container
```

Containers are named deterministically based on the workspace path. Use `yolo ps` to see running containers.

---

## Config Safety

When `yolo-jail.jsonc` changes between jail startups, the CLI shows a normalized diff and asks for confirmation:

```
Config has changed since last confirmed session.
Diff:
  + "packages": ["postgresql"]

Accept this config? [y/N]:
```

This prevents agents from silently adding packages, mounts, or devices. The human must approve every change.

### Workflow for Config Changes

**From outside the jail (handoff to agent):**
1. Edit `yolo-jail.jsonc`
2. Run `yolo check` to validate
3. Fix any errors
4. Run `yolo` to start the jail (will see diff and prompt for approval)

**From inside the jail (agent edits mid-session):**
1. Agent edits `yolo-jail.jsonc`
2. Agent runs `yolo check --no-build` for fast validation
3. Agent fixes any reported problems
4. Agent asks human to restart: _"I've updated the config. Please restart the jail."_
5. Human exits and runs `yolo` again (sees diff, approves)

See [docs/config-safety.md](config-safety.md) for the full workflow.

---

## Platform Differences Reference

YOLO Jail runs on Linux and macOS as first-class platforms. Everything in this guide works on both unless explicitly noted. The table below summarizes what differs; see [docs/platform-comparison.md](platform-comparison.md) for the full feature matrix and architecture diagrams.

| Feature | Linux | macOS Docker / Podman | macOS Apple Container |
|---------|-------|------------------------|----------------------|
| Container isolation | ✅ native | ✅ via VM | ✅ per-container VM |
| Workspace mount (`/workspace`) | Native bind | VirtioFS | VirtioFS |
| Auto-detect priority | podman → docker | container → podman → docker (via VM) | same |
| Cgroup limits (`yolo-cglimit`) | ✅ | ❌ — use VM resource controls | ✅ (own kernel) |
| Per-container CPU/memory | Via cgroups | VM-level only | ✅ native (`--cpus`, `--memory`) |
| GPU passthrough (NVIDIA) | ✅ | ❌ (no CUDA on Apple Silicon) | ❌ |
| USB / serial device passthrough | ✅ | ❌ | ❌ |
| Port publishing (`network.ports`) | ✅ | ✅ | ✅ |
| Port forwarding (`forward_host_ports`) | Unix sockets | TCP gateway (auto) | Native Unix sockets |
| `--network host` | ✅ | ✅ | ❌ (not supported) |
| UID mapping | `-u UID:GID` | VM handles automatically | VM per container |
| `mise` tool storage | Host bind mount | Docker named volume | Docker named volume |
| Max bind mounts | Unlimited | Unlimited | ~22 (VZ.framework) |
| Image format | Docker V2 | Docker V2 | OCI (auto-converted via skopeo) |
| `yolo doctor` runtime checks | Linux | macOS / VM | `container system status` |
| Token refresher install | systemd `--user` | launchd or cron (see [scripts/README.md](../scripts/README.md)) | same |

---

## Troubleshooting

Start with `yolo check` — it validates your entire setup on both platforms: runtime (podman/docker/container), nix, config, image, running containers, GPU (Linux only), macOS VM backend (macOS only), and the Claude token refresher.

```bash
yolo check                    # full check including nix build
yolo check --no-build         # fast — skip nix build
```

### Common Issues (both platforms)

**"Cannot find yolo-jail repo root"** — The CLI needs the source for nix image builds. Either clone the repo and run `just deploy` from inside it, or add `repo_path` to your user config:

```jsonc
// ~/.config/yolo-jail/config.jsonc
{ "repo_path": "~/code/yolo-jail" }
```

**Image build fails**

- Check nix is installed with flakes: `nix --version`
- Ensure flakes are enabled in `~/.config/nix/nix.conf`:
  ```
  experimental-features = nix-command flakes
  ```
- On macOS: also verify the remote Linux builder — `nix store info --store ssh-ng://nix-builder` should respond within a few seconds
- Run `yolo check` for detailed diagnostics

**Container won't start**

- Linux: check `podman --version` or `docker --version`; verify your user is in the `docker` group (for Docker)
- macOS: check that your runtime's VM/daemon is up:
  - Colima: `colima status`
  - Podman Machine: `podman machine list`
  - Apple Container: `container system status`
- Try forcing a new container: `yolo --new`
- Check for leftover containers: `yolo ps`

**MCP server not working**

- Verify the preset is enabled in `mcp_presets`
- Check logs (same paths on Linux and macOS): `~/.copilot/logs/` (Copilot), `~/.cache/gemini-cli/logs/` (Gemini), `~/.claude/logs/` (Claude)
- Inside jail, view logs: `tail -100 ~/.copilot/logs/$(ls -1t ~/.copilot/logs | head -1)`

**LSP not responding**

- LSP servers are spawned on-demand, not as background services
- Ensure the language server binary is installed (`mise ls`)
- TypeScript LSP requires `tsconfig.json` or `jsconfig.json` in the workspace root

**Tools missing after restart**

- `eval "$(mise hook-env -s bash)"` to refresh PATH
- Or restart the jail: `yolo --new`

**Permission errors on files**

- Linux + Docker: UID/GID mapping is handled via `-u UID:GID`
- Linux + Podman: Rootless UID mapping handles ownership automatically
- macOS (any runtime): File ownership is mediated by the VM's virtiofs layer; files inside `/workspace` appear as the jail user and on the host appear as you
- If persistent, check `ls -la ~/.local/share/yolo-jail/home/`

**Claude keeps logging out across jails**

- Usually means the host-side token refresher isn't running. Check with `yolo check` — it has a dedicated "Claude Token Refresher" section.
- Linux: `systemctl --user status claude-token-refresher.timer` and `journalctl --user -u claude-token-refresher -n 30`
- macOS: check your launchd/cron setup per [scripts/README.md](../scripts/README.md)
- Background: Anthropic rotates refresh tokens single-use, so multiple jails refreshing simultaneously race each other. The refresher serializes refreshes on the host so jails never race. See `docs/claude-oauth-mitm-proxy-plan.md` for the fallback plan if the refresher alone isn't enough.

### Linux-Specific Issues

**NVIDIA GPU not visible in jail**

- Check `nvidia-smi` on the host works
- Verify NVIDIA Container Toolkit: `nvidia-ctk --version`
- For Podman, ensure the CDI spec exists: `/etc/cdi/nvidia.yaml`
- See [GPU Passthrough](#gpu-passthrough-nvidia) for the full setup

**Podman rootless permission denied on `/dev/dri` or devices**

- Some device passthrough paths need `--cap-add` which Podman rootless may restrict
- Fall back to Docker for these workloads, or run Podman rootful (`sudo podman`)

### macOS-Specific Issues

**Podman Machine won't start on headless Mac (EC2, CI)**

- Apple's Hypervisor.framework may require a GUI session
- Switch to Colima + Docker: `brew install colima docker && colima start`
- Set `export YOLO_RUNTIME=docker` (or drop `YOLO_RUNTIME` — auto-detect will pick it up)

**Nix build hangs or times out**

- Check `nix store info` responds within 2 seconds
- If it hangs, kill determinate-nixd and use the vanilla daemon:
  ```bash
  sudo pkill determinate-nixd
  sudo /nix/var/nix/profiles/default/bin/nix-daemon &
  ```
- Verify the remote builder: `nix store info --store ssh-ng://nix-builder`
- After `colima start` restarts the VM, the SSH port for `nix-builder` may change — update `~/.ssh/config` accordingly

**Port forwarding not working**

- Docker/Podman on macOS: YOLO Jail uses a TCP gateway (`host.docker.internal`) instead of Unix sockets because virtiofs rejects sockets. This is automatic.
- Apple Container: uses native `--publish-socket` — no TCP gateway needed.
- Ensure `socat` is in the container (it's in the default image)

**Apple Container: "virtual machine failed to start"**

- VZ.framework caps bind mounts at ~22. YOLO Jail consolidates the workspace state into a single `/home/agent` mount to stay under this, but if you add many custom `mounts` entries you may still hit it.
- Try `YOLO_RUNTIME=podman` or `YOLO_RUNTIME=docker` to sidestep the limit.

**Apple Container: image load fails**

- Apple Container requires OCI-format images. YOLO Jail converts via `skopeo` first (no daemon needed), or `podman`/`docker` as fallback.
- If you don't have `skopeo` installed and don't have a Docker daemon running, install skopeo: `brew install skopeo`.

**`/tmp` bind mounts fail**

- macOS `/tmp` → `/private/tmp` is a symlink. `cli.py` resolves this automatically.
- With Colima, ensure the VM was started with `--mount /private/tmp:w`.

**Colima's Nix builder port changes on restart**

- Every `colima stop` + `colima start` can assign a new SSH port for the VM
- Re-run the SSH port update step from [docs/macos.md](macos.md#option-a--colima-vm-as-nix-builder-recommended-for-colima-users) after each restart
- Or use a fixed port via `colima start --ssh-port 2222` and hardcode it in `~/.ssh/config`

See [docs/macos.md](macos.md) for the full macOS-specific reference, and [docs/platform-comparison.md](platform-comparison.md) for the complete Linux-vs-macOS feature matrix.
