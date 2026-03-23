# YOLO Jail

[![CI](https://github.com/mschulkind/yolo-jail/actions/workflows/ci.yml/badge.svg)](https://github.com/mschulkind/yolo-jail/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A secure, isolated container environment for AI agents (Copilot, Gemini CLI) to safely modify codebases without compromising host security or identity. Supports both Docker and Podman runtimes.

## Why?

AI coding agents like GitHub Copilot and Google Gemini CLI have a `--yolo` mode that lets them run shell commands without confirmation. This is powerful but dangerous — agents can access your SSH keys, cloud credentials, git identity, and anything else on your machine.

**YOLO Jail** lets you run agents in YOLO mode safely by isolating them in a container with:
- ❌ No access to `~/.ssh/`, `~/.gitconfig`, or cloud credentials
- ✅ Separate auth (`gh auth login` and `gemini login` inside the jail)
- ✅ Your codebase mounted read-write at `/workspace`
- ✅ Persistent tool state across restarts
- ✅ Pre-configured MCP servers, LSP servers, and modern CLI tools

## Features

- **Isolated:** Runs in a Docker/Podman container with no access to host credentials
- **Optimized:** Pre-installed with modern, fast tools (`rg`, `fd`, `bat`, `eza`, `jq`, `delta`, `fzf`)
- **Restricted:** Blocked tools return clear errors with suggestions (e.g., `rg` instead of `grep`)
- **Reproducible:** Defined entirely via Nix Flakes
- **Agent-Ready:** MCP presets (Chrome DevTools, Sequential Thinking) and LSP servers (Pyright, TypeScript) — enable by name
- **Configurable:** Per-project config via `yolo-jail.jsonc`, user defaults via `~/.config/yolo-jail/config.jsonc`
- **Container Reuse:** Same workspace reuses the same container via `exec`
- **Runtime Flexible:** Works with both Docker and Podman (prefers Podman)

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **[Nix](https://nixos.org/download/)** (with flakes enabled)
- **[Docker](https://docs.docker.com/)** or **[Podman](https://podman.io/)**

## Installation

Requires [uv](https://docs.astral.sh/uv/), [Nix](https://nixos.org/download/) (with flakes), and [Docker](https://docs.docker.com/) or [Podman](https://podman.io/).

```bash
# Install from source
git clone https://github.com/mschulkind/yolo-jail.git
cd yolo-jail
uv tool install .

# (Optional) Set user-level defaults
yolo init-user-config
# Edit: ~/.config/yolo-jail/config.jsonc
```

To upgrade later: `cd yolo-jail && git pull && uv tool install . --force`

For development, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick Start

```bash
# Navigate to any repository
cd ~/code/my-project

# Start an interactive shell in the jail
yolo

# Or run a command directly
yolo -- copilot          # Copilot with --yolo auto-injected
yolo -- gemini           # Gemini with --yolo auto-injected

# Force a new container
yolo --new -- bash

# ALWAYS run this after every yolo-jail.jsonc edit, before restarting
yolo check

# Check your setup
yolo doctor

# List running jails
yolo ps

# Show full configuration reference
yolo config-ref
```

### First Run

On first run, YOLO Jail will:
1. Build the Docker image via `nix build` (takes a few minutes)
2. Load the image into your container runtime
3. Install MCP servers, LSP servers, and utilities
4. Start your command

Subsequent runs are fast — tools are cached in persistent storage.

### Auth Setup (One-Time)

Inside the jail, authenticate with your tools:

```bash
gh auth login          # GitHub CLI
gemini login           # Google Gemini CLI
```

These tokens are stored in `~/.local/share/yolo-jail/home/` and persist across jail restarts.

## Configuration

Create a per-project config in `yolo-jail.jsonc`:

```jsonc
{
  "runtime": "podman",              // or "docker"
  "packages": ["strace", "htop"],   // extra nix packages
  "mounts": ["/path/to/ref-repo"],  // extra read-only mounts
  "network": {
    "mode": "bridge",               // or "host" for host networking
    "ports": ["8000:8000"]          // publish ports in bridge mode
  },
  "security": {
    "blocked_tools": ["curl", "wget"]
  }
}
```

Workspace config merges over user defaults (`~/.config/yolo-jail/config.jsonc`). Lists merge and dedupe, scalars override.

Run `yolo check` after **every** edit to `yolo-jail.jsonc` to validate the merged config, dry-run the generated jail agent configs, and preflight the image build before restarting into the jail. Inside a running jail, `yolo check --no-build` is the fast way to validate config changes mid-session before asking for a restart.

Run `yolo config-ref` for the full configuration reference.

## Security

- **Strict Isolation**: No access to host `~/.ssh/`, `~/.gitconfig`, or cloud credentials
- **Separate Auth**: Run `gh auth login` and `gemini login` inside the jail once
- **User Mapping**: Files created in the jail are owned by your host user (matching UID/GID)
- **Blocked Tools**: Configurable list of tools that return clear error messages
- **Config Safety**: Changes to `yolo-jail.jsonc` require human confirmation at next startup — agents cannot silently modify the jail environment. See [docs/config-safety.md](docs/config-safety.md).
- **Read-Only Mounts**: Extra mounts are read-only by default

## Troubleshooting

Run `yolo doctor` to diagnose common setup issues:

```bash
yolo doctor
```

This checks your container runtime, Nix installation, configuration files, image status, and running containers.

Run `yolo check` after **every** config edit, especially when handing work from an outside agent into the jail or when an in-jail agent edits `yolo-jail.jsonc` mid-session and needs to verify the restart will succeed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## Documentation

- [User Guide](docs/USER_GUIDE.md) — Detailed setup, configuration, and troubleshooting
- [Config Safety](docs/config-safety.md) — How config change approval works
- [Storage & Config](docs/storage-and-config.md) — Storage hierarchy and mount layout

## License

[Apache License 2.0](LICENSE)
