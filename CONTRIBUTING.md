# Contributing to YOLO Jail

Thank you for your interest in contributing! Here's how to get started.

## Development Setup

### Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| [Python 3.13+](https://python.org) | Runtime | Your package manager |
| [Nix](https://nixos.org/download/) | Image builder | `sh <(curl -L https://nixos.org/nix/install) --daemon` |
| [uv](https://docs.astral.sh/uv/) | Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [just](https://just.systems/) | Command runner | `cargo install just` or `brew install just` |
| [Docker](https://docs.docker.com/) or [Podman](https://podman.io/) | Container runtime | Your package manager |

### Getting Started

```bash
git clone https://github.com/mschulkind/yolo-jail.git
cd yolo-jail
uv sync --group dev   # Install Python dependencies (including dev tools)
just test             # Run tests
just check            # Format, lint, and test
```

### Editable Install (for development)

```bash
# Install in editable mode — changes take effect immediately
uv sync --group dev

# The yolo CLI is available via uv run:
uv run yolo --help

# Or install as a tool for direct CLI access during dev:
uv tool install -e .
```

### Running Tests

```bash
just check    # Format, lint, and test — run this before every PR
just test     # All tests (pytest)
just lint     # Linting only (ruff)
just format   # Auto-format code (ruff)
```

## Architecture

YOLO Jail has two main components:

- **`src/cli.py`** — The host-side CLI (typer). Handles container lifecycle, config loading, image building, and `docker`/`podman` invocation.
- **`src/entrypoint.py`** — The container-side startup script. Generates shell config, shims, MCP/LSP configs, and the bootstrap script. Uses stdlib-only Python (no pip dependencies).

All logic is **pure Python** — no bash scripts with embedded heredocs. Bash is only generated as *content* (shim scripts, .bashrc) written by Python.

### Key Design Principles

- **No host credentials leak** — The jail never accesses `~/.ssh`, `~/.gitconfig`, or cloud tokens
- **Persistent tool state** — Installed tools survive jail restarts via `~/.local/share/yolo-jail/`
- **Container reuse** — Same workspace reuses the same container via `exec`
- **Self-bootstrapping** — The project is developed from inside its own jail

## Making Changes

### Coding Standards

- Type hints on all function signatures
- Follow PEP 8 (enforced by `ruff`)
- Keep functions focused and well-named
- Use `shlex.join` for shell command construction
- Use `rich` for terminal output formatting

### Commit Messages

Use conventional commit style:

```
feat: add doctor command for environment health checks
fix: handle missing nix daemon socket gracefully
docs: update configuration reference
```

## Versioning

YOLO Jail follows [Semantic Versioning](https://semver.org/):

- **MAJOR** (x.0.0) — breaking changes to CLI, config format, or container behavior
- **MINOR** (0.x.0) — new features, backward-compatible
- **PATCH** (0.0.x) — bug fixes, documentation, internal improvements

While in 0.x.y, the API is not considered stable and minor versions may include breaking changes.

## Pull Request Process

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes with tests
4. Run `just check` and ensure everything passes
5. Submit a PR with a clear description of what and why

### What Makes a Good PR

- **Small and focused** — one logical change per PR
- **Tested** — new features have tests, bug fixes include regression tests
- **Documented** — update docs if behavior changes

## Bug Reports

Please include:
- Steps to reproduce
- Expected vs actual behavior
- YOLO Jail version (`yolo --help` header)
- OS, container runtime, and Python version

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
