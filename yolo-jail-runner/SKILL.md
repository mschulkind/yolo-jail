---
name: yolo-jail-runner
description: Manage and run the YOLO Jail environment for safe, isolated execution of AI agents. Use this skill when the user wants to start a secure jail session, configure blocked utilities, or manage jail settings for a specific project.
---

# YOLO Jail Runner

This skill manages the configuration and execution of the YOLO Jail, a secure Docker-based environment for AI agents.

## Core Functions

### 1. Configure Jail
Before running, you can configure blocked utilities. Create a `yolo-jail.jsonc` in the target project root (not the jail repo itself).

**`yolo-jail.jsonc` Example:**
```jsonc
{
  "security": {
    "blocked_tools": ["grep", "find", "curl"] // Add tools to block
  },
  "network": {
    "mode": "bridge"
  }
}
```

### 2. Start Jail
Run the jail using the global `yolo` command or the `Justfile` in the jail repository.

```bash
# Run globally (if installed)
yolo

# Run from jail repo
just run-path <target-path>
```

### 3. Authentication
The jail uses an **isolated identity**. It does NOT share your host's SSH keys or general Git credentials.
- **Allowed:** GitHub Copilot, Gemini CLI (via one-time login inside the jail).
- **Blocked:** General SSH keys, GPG keys, other cloud creds (unless explicitly added).

## Best Practices
- **Project-Specific Tools:** Use `mise.toml` in the target project to define tools (Node, Python, etc.).
- **One-Time Auth:** Run `gh auth login` and `gemini login` once inside the jail; they persist globally for the jail user.