# YOLO Jail User Guide

This guide covers everything you need to get started with YOLO Jail and make the most of its features. For quick-start instructions, see the [README](../README.md).

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
- [Storage & Persistence](#storage--persistence)
- [Container Reuse](#container-reuse)
- [Config Safety](#config-safety)
- [Troubleshooting](#troubleshooting)

---

## Installation

### Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| [uv](https://docs.astral.sh/uv/) | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Nix](https://nixos.org/download/) | Image builder (with flakes) | `sh <(curl -L https://nixos.org/nix/install) --daemon` |
| [Docker](https://docs.docker.com/) or [Podman](https://podman.io/) | Container runtime | Your package manager |

### Install

```bash
git clone https://github.com/mschulkind/yolo-jail.git
cd yolo-jail
uv tool install .
```

To upgrade later:

```bash
cd yolo-jail && git pull && uv tool install . --force
```

### Set Up User Defaults (Optional)

```bash
yolo init-user-config
# Edit: ~/.config/yolo-jail/config.jsonc
```

User-level defaults apply to all projects and are merged under workspace config.

---

## First Run

Navigate to any repository and run:

```bash
cd ~/code/my-project
yolo
```

On first run, YOLO Jail will:

1. **Build the Docker image** via `nix build` — this takes a few minutes the first time. Nix caches the result, so subsequent builds are fast unless the package list changes.
2. **Load the image** into your container runtime (Docker or Podman).
3. **Install tools** — MCP servers, LSP servers, and utilities are installed into persistent storage.
4. **Start your command** — by default, an interactive shell.

Subsequent runs skip steps 1–3 (everything is cached) and start in seconds.

---

## Authentication

Inside the jail, authenticate with your tools once:

```bash
gh auth login          # GitHub CLI
gemini login           # Google Gemini CLI
```

Tokens are stored in `~/.local/share/yolo-jail/home/` on the host and persist across jail restarts. You do **not** need to re-authenticate each time.

> **Security note:** Auth tokens are stored separately from your host credentials. The jail never accesses your host `~/.ssh/`, `~/.gitconfig`, or cloud credentials.

---

## CLI Commands

### `yolo` — Start a Jail

```bash
yolo                       # Interactive shell
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
  // Container runtime
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

YOLO Jail configures LSP (Language Server Protocol) servers for both Copilot and Gemini. Three servers are always available:

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

Pass host devices (USB, serial, etc.) into the jail:

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

Train deep learning models inside the jail using NVIDIA GPUs. Requires the NVIDIA Container Toolkit on the host.

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
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### Runtime Details

| Runtime | Mechanism | Notes |
|---------|-----------|-------|
| **Docker** | `--gpus all` | Requires `nvidia-ctk runtime configure --runtime=docker` |
| **Podman** | `--device nvidia.com/gpu=all` (CDI) | Requires CDI spec at `/etc/cdi/nvidia.yaml` |

- **Podman rootless:** GPU passthrough uses `--userns=keep-id` instead of `--uidmap` (nested podman-in-podman is not available when GPU is active).
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

- **`conmon bytes "": readObjectStart` error (Podman):** Caused by `--uidmap` conflicting with CDI. Fixed in current release. If you see this, update your `yolo-jail` installation.
- **`nvidia-smi` not found inside jail:** The NVIDIA Container Toolkit injects driver libs at container start. Check the toolkit is installed and configured on the host.
- **CUDA out of memory:** Reduce batch size, or limit which GPUs are exposed with `"devices": "0"`.

---

## Storage & Persistence

### What Persists Across Restarts

| Data | Location (Host) | Shared? |
|------|-----------------|---------|
| Auth tokens (gh, gemini) | `~/.local/share/yolo-jail/home/` | All jails |
| Installed tools (npm, go) | `~/.local/share/yolo-jail/home/` | All jails |
| Mise tools & runtimes | `~/.local/share/mise/` | All jails + host |
| Bash history | `<workspace>/.yolo/home/bash_history` | Per workspace |
| Copilot sessions | `<workspace>/.yolo/home/copilot-sessions/` | Per workspace |
| Gemini history | `<workspace>/.yolo/home/gemini-history/` | Per workspace |
| SSH keys | `<workspace>/.yolo/home/ssh/` | Per workspace |

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

## Troubleshooting

### Run the Health Check

```bash
yolo check
```

This validates your entire setup: runtime, nix, config, image, and running containers.

### Common Issues

**"Cannot find yolo-jail repo root"**
The CLI needs the source for nix image builds. Either:
- Clone the repo and run `uv tool install .` from inside it, or
- Add `repo_path` to your user config:
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
- Run `yolo check` for detailed diagnostics

**Container won't start**
- Check your runtime: `docker --version` or `podman --version`
- Try forcing a new container: `yolo --new`
- Check for leftover containers: `yolo ps`

**MCP server not working**
- Verify the preset is enabled in `mcp_presets`
- Check logs: `~/.copilot/logs/` (Copilot) or `~/.cache/gemini-cli/logs/` (Gemini)
- Inside jail, view logs:
  ```bash
  tail -100 ~/.copilot/logs/$(ls -1t ~/.copilot/logs | head -1)
  ```

**LSP not responding**
- LSP servers are spawned on-demand, not as background services
- Ensure the language server binary is installed (check `mise ls`)
- TypeScript LSP requires `tsconfig.json` or `jsconfig.json` in the workspace root

**Tools missing after restart**
- Run `eval "$(mise hook-env -s bash)"` to refresh PATH
- Or restart the jail: `yolo --new`

**Permission errors on files**
- Docker: UID/GID mapping is handled via `-u UID:GID`
- Podman: Rootless UID mapping handles ownership automatically
- If persistent, check `ls -la ~/.local/share/yolo-jail/home/`
