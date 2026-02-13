# YOLO Jail: Agent Developer Guide

This project provides a secure, isolated Docker environment for AI agents (Gemini CLI, Copilot) to execute commands on local repositories without compromising host security or identity.

## Architectural Specs

### 1. Configuration (`yolo-jail.jsonc`)
- **Format**: JSON with comments (JSONC). **TOML is deprecated**.
- **Location**: Project root.
- **Dynamic Shims**: Blocked tools (grep, find, etc.) are generated dynamically based on this config. 
- **Smart Shims**: `grep` and `find` shims allow background scripts but block interactive TTY usage to prevent agents from wasting tokens on huge recursive searches.
- **Extra Mounts**: The `mounts` array brings additional host paths into the jail read-only at `/ctx/<basename>` (or a custom container path via `"host:container"` syntax).

### 2. Isolation & Identity
- **Strict Isolation**: The jail MUST NOT access host `~/.ssh/`, `~/.gitconfig`, or any cloud credentials.
- **Login inside the Jail**: Users must perform a one-time `gh auth login` and `gemini login` within the jail.
- **Persistent Global State**: All jail-specific state (auth tokens, bash history, global tool cache) is stored in `~/.local/share/yolo-jail/`.
    - Host `~/.local/share/yolo-jail/home` -> Container `/home/agent`
    - Host `~/.local/share/yolo-jail/mise` -> Container `/mise`

### 3. Execution Engine (`src/cli.py` & `src/entrypoint.sh`)
- **Direct Execution**: Commands are run via `yolo -- <command>`. 
- **Auto-YOLO**: The CLI automatically injects `--yolo` for `gemini` and `copilot` commands.
- **Quoting**: Use `shlex.join` in Python to pass quoted arguments correctly to the container's `bash -c`.
- **Self-Updating Build**: The CLI runs `nix build` on every start but only executes `docker load` if the resulting image hash differs from `.last-load`. This makes updates cheap and automatic.

## Developer Runbook

### Debugging MCP & LSP
- **Logs**: Copilot logs are in `~/.config/.copilot/logs/`.
- **Chromium Stability**: Headless Chromium in Docker is brittle. 
    - **Connect Mode**: Chrome DevTools MCP uses a wrapper script (`~/.local/bin/chrome-devtools-mcp-wrapper`) that pre-launches Chromium with `--remote-debugging-port` and connects via `--browser-url`. This avoids pipe-mode fd conflicts when MCP servers are spawned by agents.
    - **Required Chrome Flags**: `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`, `--disable-software-rasterizer`.
    - **Docker**: Use `--shm-size=2g` in the Docker run command for adequate shared memory.
    - **Binary Discovery**: Always use absolute paths (e.g., `/usr/bin/chromium`) in MCP configs.
- **LSP Schemas**:
    - **Copilot**: Requires `mcpServers` (plural) key in `mcp-config.json` and a separate `lsp-config.json` with `fileExtensions`.
    - **Gemini**: Uses `mcpServers` key in `settings.json`.

### Tool Management
- **Mise**: All runtimes (Node@22, Python@3.13, Go) are managed by `mise`. 
- **Bootstrapping**: MCP servers are installed via `npm install -g` or `go install` into the persistent `/home/agent` partition during the container's startup (`~/.yolo-bootstrap.sh`).
- **Binary Locations**:
    - NPM Globals: `/home/agent/.npm-global/bin/`
    - Go Binaries: `/home/agent/go/bin/`
- **PATH Order**: `${SHIM_DIR}:/home/agent/.npm-global/bin:/home/agent/go/bin:/mise/shims:/bin:/usr/bin`.

### Environment Hygiene
- **No Pagers**: Agents cannot handle interactive pagers.
    - `PAGER=cat`, `GIT_PAGER=cat`, `BAT_PAGER=""`.
    - `alias bat='bat --style=plain --paging=never'`.
- **Terminal**: `TERM=xterm-256color` should be passed to maintain color support for agent parsing.
- **Permissions**: Map host UID/GID to the container user to ensure file ownership on the host is preserved.

## Workflow for Modification
1. **Change Image**: Edit `flake.nix` (e.g., add `pkgs.strace`).
2. **Change Logic**: Edit `src/entrypoint.sh` or `src/cli.py`.
3. **Automatic Test**: Run `uv run pytest tests/test_jail.py`.
4. **Manual Test**: Run `yolo -- bash -c "my-new-tool --version"`.
5. **Enforce YOLO**: Always ensure `YOLO_BYPASS_SHIMS=1` is set when running installers inside the jail.
6. **Commit & Push**: Always commit and push after every change. The Nix image is built from the working tree, and other users need the latest code.