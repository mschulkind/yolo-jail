# YOLO Jail

A restricted, secure Docker environment designed for AI agents (like VS Code Copilot and Gemini) to safely modify codebases.

## Features

- **Isolated:** Runs in a minimal Docker container.
- **Optimized:** Pre-installed with modern, fast tools:
    - `rg` (ripgrep)
    - `fd`
- **Restricted:** Dangerous or slow legacy tools are blocked or shimmed:
    - `grep` -> Redirects to `rg`
    - `find` -> Redirects to `fd`
- **Reproducible:** Defined entirely via Nix Flakes.

## Global Usage

You can use YOLO Jail as a secure environment for any project on your machine.

### 1. "Install" the Global Command
Run this once to create a `yolo` command in your path:
```bash
sudo ln -s $(pwd)/yolo-enter.sh /usr/local/bin/yolo
```

### 2. Enter any Project
Navigate to any repository and type:
```bash
yolo
```
The jail will launch, mounting your current directory to `/workspace`. It will share your global `gh` and `gemini-cli` authentication, and tools will be persistent across sessions.

## Tool Management (Mise)

This project uses **Mise** to manage project-specific tools.
- To add a tool to a project, create or edit `mise.toml` in that project's root.
- `gemini-cli@0.27.3` is pinned in this repository's `mise.toml`.

## Security & Safety

- **Isolation**: Docker prevents the agent from touching your host filesystem.
- **Read-Only Auth**: Your `~/.config/gh` and `~/.config/gemini-cli` are mounted as **Read-Only**.
- **Fail Loudly**: Legacy tools like `grep` and `find` are shimmed to redirect you to faster, modern alternatives (`rg`, `fd`).
- **User Mapping**: Files created in the jail are owned by your host user (matching UID/GID).
