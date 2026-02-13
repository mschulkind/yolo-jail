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
# To open an interactive shell:
yolo

# To run a command directly and exit:
yolo gemini prompt "What is this repo?"
yolo copilot
```
The jail will launch, mounting your current directory to `/workspace`. It will share your global `gh` and `gemini-cli` authentication, and tools will be persistent across sessions.

### 3. First-Time Authentication
The jail uses an **Isolated Identity**. You must log in once inside the jail for your tools to work. These credentials persist across all projects you use with YOLO Jail.
```bash
# Inside the jail:
gh auth login
gemini login
```

## Project Configuration (`yolo-jail.jsonc`)

You can customize the jail's behavior for a specific project.

### 1. Initialize Config
Run this in your project root to generate a default configuration:
```bash
yolo init
```

### 2. Configuration Options
Edit the generated `yolo-jail.jsonc`:
```jsonc
{
  "security": {
    // Tools that the agent is strictly forbidden from using.
    // Can be a string or an object with custom messages.
    "blocked_tools": [
      "curl", 
      "wget",
      {
        "name": "grep",
        "message": "Use 'rg' (ripgrep) for faster searching.",
        "suggestion": "rg <pattern>"
      }
    ]
  },
  "network": {
    // \"bridge\" (default): Isolated network with own IP.
    // \"host\": Shares host network (useful for servers, but less secure).
    "mode": "bridge",

    // For bridge mode, publish ports to host [\"Host:Container\"]
    // \"ports\": [\"8000:8000\", \"3000:3000\"]
  }
}
```

### 3. Networking
- **Default**: The jail runs in `bridge` mode. It has its own IP.
- **Host Mode**: Use `yolo --network host` or set `mode = "host"` in config.

## Tool Management (`mise.toml`)

YOLO Jail uses **Mise** to manage runtimes (Node, Python, etc.). If your project has a `mise.toml`, the jail will automatically install those tools when you enter.

## Security Features

1.  **Filesystem Isolation**: The agent only sees the project folder (`/workspace`) and a private home directory.
2.  **Credential Sandboxing**:
    *   **NO SSH Keys**: The jail cannot see your host's SSH keys.
    *   **NO Git Config**: The jail does not share your host's global `.gitconfig`.
    *   **Private Auth**: All tokens for GitHub and Gemini are stored in a dedicated folder separate from your host config.
3.  **Fail-Loudly Shims**: If an agent tries to use a blocked tool (like `grep`), it receives an error message explaining *why* and suggesting a faster alternative (like `ripgrep`).
