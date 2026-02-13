# YOLO Jail: Human User Guide

YOLO Jail is a secure, high-performance environment for running AI agents (like Gemini CLI and GitHub Copilot) on your local repositories without giving them full access to your machine.

## Quick Start

### 1. Installation
Run this command once in the `yolo_jail` repository to create a global shortcut:
```bash
sudo ln -s $(pwd)/yolo-enter.sh /usr/local/bin/yolo
```

### 2. Enter a Project
Navigate to any directory you want the agent to work in and type:
```bash
yolo
```

### 3. First-Time Authentication
The jail uses an **Isolated Identity**. You must log in once inside the jail for your tools to work. These credentials persist across all projects you use with YOLO Jail.
```bash
# Inside the jail:
gh auth login
gemini login
```

## Project Configuration (`yolo-jail.toml`)

You can customize the jail's behavior for a specific project by placing a `yolo-jail.toml` file in the project's root directory.

### Example Configuration
```toml
[security]
# Tools that the agent is strictly forbidden from using
blocked_tools = ["curl", "wget", "ping", "grep", "find"]

# Note: grep and find are blocked by default to encourage using 'rg' and 'fd'
```

## Tool Management (`mise.toml`)

YOLO Jail uses **Mise** to manage runtimes (Node, Python, etc.). If your project has a `mise.toml`, the jail will automatically install those tools when you enter.

## Security Features

1.  **Filesystem Isolation**: The agent only sees the project folder (`/workspace`) and a private home directory.
2.  **Credential Sandboxing**:
    *   **NO SSH Keys**: The jail cannot see your host's SSH keys.
    *   **NO Git Config**: The jail does not share your host's global `.gitconfig`.
    *   **Private Auth**: All tokens for GitHub and Gemini are stored in a dedicated folder separate from your host config.
3.  **Fail-Loudly Shims**: If an agent tries to use a blocked tool (like `grep`), it receives an error message explaining *why* and suggesting a faster alternative (like `ripgrep`).
