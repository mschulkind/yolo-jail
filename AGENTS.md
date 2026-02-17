# YOLO Jail: Agent Developer Guide

This project provides a secure, isolated container environment for AI agents (Gemini CLI, Copilot) to execute commands on local repositories without compromising host security or identity. Supports both Docker and Podman runtimes.

## Architectural Specs

### 1. Configuration (`yolo-jail.jsonc`)
- **Format**: JSON with comments (JSONC). **TOML is deprecated**.
- **Location**: Project root.
- **User Defaults**: Optional global config at `~/.config/yolo-jail/config.jsonc` (create with `yolo init-user-config`).
- **Merge Rules**: Workspace config is merged over user config. Lists are merged+deduped; scalar/object values in workspace override user defaults.
- **Dynamic Shims**: Blocked tools are generated dynamically based on this config. All blocked tools are unconditionally blocked unless `YOLO_BYPASS_SHIMS=1` is set.
- **Custom Packages**: The `packages` array specifies additional nix packages to bake into the jail image. Names must match nixpkgs attribute names. The image only rebuilds when this list changes. Uses `--impure` nix build with `builtins.getEnv`.
- **Extra Mounts**: The `mounts` array brings additional host paths into the jail read-only at `/ctx/<basename>` (or a custom container path via `"host:container"` syntax).
- **Runtime Selection**: The `"runtime"` key selects the container runtime (`"podman"` or `"docker"`). Can also be set via `YOLO_RUNTIME` env var. Priority: env var > workspace config > user config > auto-detect (prefers podman).

### 2. Isolation & Identity
- **Strict Isolation**: The jail MUST NOT access host `~/.ssh/`, `~/.gitconfig`, or any cloud credentials.
- **Login inside the Jail**: Users must perform a one-time `gh auth login` and `gemini login` within the jail.
- **Persistent Global State**: All jail-specific state (auth tokens, bash history, global tool cache) is stored in `~/.local/share/yolo-jail/`.
    - Host `~/.local/share/yolo-jail/home` -> Container `/home/agent`
    - Host `~/.local/share/yolo-jail/mise` -> Container `/mise`

### 3. Execution Engine (`src/cli.py` & `src/entrypoint.sh`)
- **Direct Execution**: Commands are run via `yolo -- <command>`. 
- **Auto-YOLO**: The CLI automatically injects `--yolo` for `gemini` and `copilot` commands.
- **Container Reuse**: By default, running `yolo` in the same workspace reuses the existing container via `exec` instead of creating a new one. Containers are named deterministically (`yolo-<hash>`) based on the workspace path. Use `yolo --new -- <command>` to force a new container. Use `yolo ps` to list active jails with their workspace mappings. Tracking files are stored in `~/.local/share/yolo-jail/containers/`.
- **Quoting**: Use `shlex.join` in Python to pass quoted arguments correctly to the container's `bash -c`.
- **Self-Updating Build**: The CLI runs `nix build --impure` on every start but only executes `<runtime> load` if the resulting image hash differs from `.last-load`. The `--impure` flag allows reading the `YOLO_EXTRA_PACKAGES` env var for per-project package customization.
- **Runtime Differences**: Docker uses `-u UID:GID` and `--net=bridge` explicitly. Podman rootless omits both (rootless UID mapping handles ownership, pasta networking avoids nftables).
- **Nested Containers (Podman-in-Podman)**: The jail image includes `podman`, `nix`, `fuse-overlayfs`, `slirp4netns`, and `shadow`. When running with podman, the CLI automatically adds UID/GID mappings (`--uidmap`/`--gidmap`), `/dev/fuse`, and `SYS_ADMIN`+`MKNOD` capabilities for rootless nested container support — no `--privileged` needed. Inner containers must use `--net=host` and `--cgroups=disabled` (configured as defaults in the image's `/etc/containers/containers.conf`).
- **AGENTS Injection**: Per-workspace AGENTS.md is generated host-side by `cli.py` and stored at `~/.local/share/yolo-jail/agents/<container-name>/AGENTS.md`. It is mounted read-only over `~/.copilot/AGENTS.md` and `~/.gemini/AGENTS.md` inside the container using nested Docker volume mounts. This ensures each workspace jail gets its own context without stomping the shared home directory, and outside-jail agents never see jail-specific instructions.
- **Skills Auto-Mount**: Host user-level skills from `~/.gemini/skills/` (which `~/.copilot/skills` typically symlinks to) are automatically mounted and synced into the jail at `/home/agent/.copilot/skills/`. If a workspace has `.copilot/skills/`, those skills are also synced and take precedence. Symlinks in skill directories are followed automatically.

## Developer Runbook

### First Run vs Subsequent Runs
- **First Run**: When you run `yolo -- <command>`, the jail entrypoint automatically provisions all tools:
  1. Builds the Docker image via `nix build --impure` (if config changed)
  2. Loads the image into Docker (if hash differs from `.last-load`)
  3. Runs the bootstrap script to install MCP servers, language servers, and utilities
  4. Executes your command
  - This takes longer (npm/go installs + potential image rebuild)
- **Subsequent Runs**: Tools are cached in persistent storage (`~/.local/share/yolo-jail/home`), so:
  1. Bootstrap script runs but skips installation (tools already exist)
  2. Your command executes immediately
  - Much faster than first run

### Debugging MCP & LSP
- **Logs**: 
  - **Copilot**: Inside jail: `~/.copilot/logs/`. On host: `~/.local/share/yolo-jail/home/.copilot/logs/`.
  - **Gemini**: Inside jail: `~/.cache/gemini-cli/logs/`. On host: `~/.local/share/yolo-jail/home/.cache/gemini-cli/logs/`.
- **Viewing Logs from Inside Jail**:
  ```bash
  # List recent logs
  yolo -- bash -lc 'ls -lt ~/.copilot/logs/ | head -5'
  
  # View latest log
  yolo -- bash -lc 'tail -100 ~/.copilot/logs/$(ls -1t ~/.copilot/logs | head -1)'
  
  # Watch logs in real-time (open in one tmux pane, run copilot in another)
  yolo -- bash -lc 'tail -f ~/.copilot/logs/$(ls -1t ~/.copilot/logs | head -1)'
  
  # Search for MCP errors
  yolo -- bash -lc 'grep -i "MCP\|Failed\|Error" ~/.copilot/logs/$(ls -1t ~/.copilot/logs | head -1)'
  ```
- **Common MCP Errors**:
  - `libstdc++.so.6: cannot open shared object file`: Node wrapper not used or `LD_LIBRARY_PATH` stripped. Check MCP config uses `/home/agent/.local/bin/mcp-wrappers/node`. The chrome-devtools wrapper sets its own `LD_LIBRARY_PATH` to be self-contained.
  - `Cannot find module '/bin/chrome-devtools-mcp'`: The chrome wrapper failed to resolve NPM_CONFIG_PREFIX. This means `$HOME` or `$NPM_CONFIG_PREFIX` wasn't set in the spawned environment.
  - `Protocol error (Target.setDiscoverTargets): Target closed`: Chrome DevTools MCP often hits this when reusing the persistent Chrome profile. Use `--isolated` in MCP args so each session gets a fresh temp profile.
  - `Runtime.callFunctionOn timed out` on complex pages: typically caused by missing fontconfig defaults in Nix images. Ensure `/etc/fonts` is present and `FONTCONFIG_FILE/FONTCONFIG_PATH` are set.
  - `Connection closed`: MCP server crashed or failed to start. Check server binary is installed (`npm list -g` inside jail).
  - `argument list too long`: Shim conflict or PATH issue. Check `.local/bin/` is not in PATH (should only be used by absolute MCP paths).
- **Config Locations**:
  - **Copilot**: `~/.copilot/config.json` (main), `~/.copilot/mcp-config.json` (MCP servers), `~/.copilot/lsp-config.json` (LSP servers).
  - **Gemini**: `~/.gemini/settings.json` (all config including MCP/LSP).
- **Workspace MCP Shadowing**: The CLI shadows any workspace `.vscode/mcp.json` with `/dev/null` inside the jail so agents only use the jail's MCP config. Host VS Code MCP configs won't interfere.
- **Chromium Stability**: Headless Chromium in Docker is brittle. 
    - **Launch Mode**: Chrome DevTools MCP launches Chromium directly via Puppeteer's `pipe: true` mode with `--headless --isolated --executablePath /usr/bin/chromium`. Docker-required flags (`--no-sandbox`, `--disable-dev-shm-usage`, etc.) are passed via `--chrome-arg=...`.
    - **Required Chrome Flags**: `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-dev-shm-usage`, `--disable-gpu`, `--disable-software-rasterizer`.
    - **Docker**: Use `--shm-size=2g` in the Docker run command for adequate shared memory.
    - **Binary Discovery**: Always use absolute paths (e.g., `/usr/bin/chromium`) in MCP configs.
- **LSP Config Format**:
    - **Copilot**: Uses `~/.copilot/lsp-config.json` with `lspServers` (plural) key.
    - **Gemini**: Uses `lspServers` key in `~/.gemini/settings.json`.
    - **Format**: `fileExtensions` must be an object mapping extensions to language IDs, e.g., `{".py": "python", ".pyi": "python"}`, not an array.
    - **Lazy Loading**: LSP servers are spawned on-demand when Copilot/Gemini analyze code files, not as persistent background services.
    - **Testing**: To verify LSP works, ask the agent to analyze a file with type errors: `@file.py check for type errors`.
    - **TypeScript Requirements**: TypeScript LSP requires a `tsconfig.json` or `jsconfig.json` in the workspace root. Without it, typescript-language-server throws `ThrowNoProject` errors. Python LSP (pyright) works without configuration.
- **Node Wrappers**: `~/.local/bin/mcp-wrappers/node` and `npx` are wrapper scripts that set `LD_LIBRARY_PATH` and fontconfig defaults before calling the mise-installed binary. MCP configs use absolute paths to these wrappers.
- **Self-Contained Wrappers**: MCP wrapper scripts (node, npx) set their own runtime env (`LD_LIBRARY_PATH`, `FONTCONFIG_*`) and use `$HOME`-relative paths instead of calling `npm config` at runtime. This ensures they work even when agents sanitize the environment. Never use `subprocess` or `npm config get` in wrapper scripts.

### Tool Management
- **Mise**: All runtimes (Node, Python, Go) are managed by `mise`. 
- **Auto-Provisioning**: On every jail start, the CLI runs `~/.yolo-bootstrap.sh` with `YOLO_BYPASS_SHIMS=1` (to avoid shim interference) before executing the user's command. Tools are installed only if missing (idempotent), so subsequent runs skip installation and rely on cached binaries in persistent storage.
  - **NPM Globals**: `chrome-devtools-mcp`, `@modelcontextprotocol/server-sequential-thinking`, `pyright`, `typescript-language-server`, `typescript`
  - **Go Binaries**: `mcp-language-server` (used by Gemini LSP)
  - **Python**: `showboat` (if pip is available)
- **Persistent Storage**: All installed binaries live in `~/.local/share/yolo-jail/home/` on the host, so they survive jail restarts and are reused without reinstalling.
- **Binary Locations**:
    - NPM Globals: `/home/agent/.npm-global/bin/`
    - Go Binaries: `/home/agent/go/bin/`
    - MCP Node Wrappers: `/home/agent/.local/bin/mcp-wrappers/`
- **PATH Order**: `${SHIM_DIR}:/home/agent/.npm-global/bin:/home/agent/go/bin:/mise/shims:/bin:/usr/bin`.

### Environment Hygiene
- **No Pagers**: Agents cannot handle interactive pagers.
    - `PAGER=cat`, `GIT_PAGER=cat`, `BAT_PAGER=""`.
    - `alias bat='bat --style=plain --paging=never'`.
- **Terminal**: `TERM=xterm-256color` should be passed to maintain color support for agent parsing.
- **Permissions**: Map host UID/GID to the container user to ensure file ownership on the host is preserved.
- **No LD_LIBRARY_PATH Stripping**: `LD_LIBRARY_PATH=/lib:/usr/lib` is baked into the Docker image Env to survive agent environment sanitization.
- **Tmux Window Title**: `PROMPT_COMMAND` is set to continuously update the tmux window title to `JAIL <dirname>`. This overrides tmux's `automatic-rename` feature which would otherwise show the current process name (e.g., "node", "python"). The title updates on every prompt, ensuring it stays as "JAIL <dirname>" even when running long-running processes.

## Workflow for Modification
1. **Change Image**: Edit `flake.nix` (e.g., add `pkgs.strace`).
2. **Change Logic**: Edit `src/entrypoint.sh` or `src/cli.py`.
3. **Automatic Test**: Run `uv run pytest tests/test_jail.py`.
4. **Manual Test**: Run `yolo -- bash -c "my-new-tool --version"`.
5. **Enforce YOLO**: Always ensure `YOLO_BYPASS_SHIMS=1` is set when running installers inside the jail.
6. **Commit & Push**: Always commit and push after every change. The Nix image is built from the working tree.

## Testing Guidelines
- **Model**: When testing Copilot interactively, always use the `gpt-4.1` model (e.g., `copilot --model gpt-4.1`). Do not use expensive models.
- **No Agent Tests**: Automated tests (`uv run pytest`) must NOT run `copilot` or `gemini` interactively. Tests may check they are installed (`--version`) but must never start interactive sessions or make API calls.
- **Manual Agent Testing**: Always test agent functionality manually before committing. Use `yolo -- copilot --yolo` or `yolo -- gemini --yolo` from a test project.
