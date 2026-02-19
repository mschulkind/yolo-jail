# YOLO Jail: Agent Developer Guide

This project provides a secure, isolated container environment for AI agents (Gemini CLI, Copilot) to execute commands on local repositories without compromising host security or identity. Supports both Docker and Podman runtimes.

## Architectural Specs

### 1. Configuration (`yolo-jail.jsonc`)
- **Format**: JSON with comments (JSONC).
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
    - Host `~/.local/share/yolo-jail/home` → Container `/home/agent` (auth, tools, configs — shared across all workspaces)
    - Host `~/.local/share/yolo-jail/mise` → Container `/mise`
    - Per-workspace overlays: `<workspace>/.yolo/home/copilot-sessions` → `/home/agent/.copilot/session-state`, `<workspace>/.yolo/home/copilot-command-history` → `/home/agent/.copilot/command-history-state.json`, `<workspace>/.yolo/home/bash_history` → `/home/agent/.bash_history`, `<workspace>/.yolo/home/gemini-history` → `/home/agent/.gemini/history`

### 3. Execution Engine (`src/cli.py` & `src/entrypoint.py`)

All logic is **pure Python** — no bash scripts with embedded heredocs. The only bash is generated *content* (shim scripts, .bashrc) written by Python.

- **Architecture**: `cli.py` runs on the host (typer CLI). `entrypoint.py` runs inside the container at startup (stdlib-only Python, no pip deps).
- **Self-Bootstrapping**: The jail is developed from inside itself. Changes to source files are immediately visible (bind-mounted workspace). Changes to `flake.nix` or `entrypoint.py` require a nix rebuild on the next `yolo` invocation from the host.
- **Direct Execution**: Commands are run via `yolo -- <command>`.
- **Auto-YOLO**: The CLI automatically injects `--yolo` for `gemini` and `copilot` commands.
- **Container Reuse**: By default, running `yolo` in the same workspace reuses the existing container via `exec` instead of creating a new one. Containers are named deterministically (`yolo-<hash>`) based on the workspace path. Use `yolo --new -- <command>` to force a new container. Use `yolo ps` to list active jails with their workspace mappings. Tracking files are stored in `~/.local/share/yolo-jail/containers/`.
- **Quoting**: Use `shlex.join` in Python to pass quoted arguments correctly to the container's `bash -c`.
- **Self-Updating Build**: The CLI runs `nix build --impure` on every start but only executes `<runtime> load` if the resulting image hash differs from `.last-load`. The `--impure` flag allows reading the `YOLO_EXTRA_PACKAGES` env var for per-project package customization.
- **Runtime Differences**: Docker uses `-u UID:GID` and `--net=bridge` explicitly. Podman rootless omits both (rootless UID mapping handles ownership, pasta networking avoids nftables).
- **Nested Containers (Podman-in-Podman)**: The jail image includes `podman`, `nix`, `fuse-overlayfs`, `slirp4netns`, and `shadow`. When running with podman, the CLI automatically adds UID/GID mappings (`--uidmap`/`--gidmap`), `/dev/fuse`, and `SYS_ADMIN`+`MKNOD` capabilities for rootless nested container support — no `--privileged` needed. When already inside a container, the CLI detects this (`/run/.containerenv` or `/.dockerenv`) and uses `--userns=host` instead of UID/GID mapping to share the parent's user namespace — doubly-nested user namespaces fail on `/proc` mount. Inner containers must use `--net=host` and `--cgroups=disabled` (configured as defaults in the image's `/etc/containers/containers.conf`). The CLI also forces `--net=host` when inside a container since netavark can't create network namespaces without `NET_ADMIN`.
- **Nix Builds Inside Jail**: When the host has a nix daemon (`/nix/var/nix/daemon-socket`), the CLI automatically mounts it plus `/nix/store:ro` and sets `NIX_REMOTE=daemon`. This forces nix inside the jail to delegate builds to the host daemon (which has nixbld users and permissions), avoiding the "build users group has no members" error. The read-only store mount provides cache hits; new derivations built by the host daemon are visible through the bind mount.
- **AGENTS Injection**: Per-workspace AGENTS.md is generated host-side by `cli.py` and stored at `~/.local/share/yolo-jail/agents/<container-name>/AGENTS.md`. It is mounted read-only over `~/.copilot/AGENTS.md` and `~/.gemini/AGENTS.md` inside the container using nested Docker volume mounts. This ensures each workspace jail gets its own context without stomping the shared home directory, and outside-jail agents never see jail-specific instructions.
- **Skills Auto-Mount**: Host user-level skills from `~/.gemini/skills/` (which `~/.copilot/skills` typically symlinks to) are automatically mounted and synced into the jail at `/home/agent/.copilot/skills/`. If a workspace has `.copilot/skills/`, those skills are also synced and take precedence. Symlinks in skill directories are followed automatically.

## Developer Runbook

### Self-Bootstrapping Development
This project is developed **from inside the jail itself**. The source code is bind-mounted at `/workspace`, so edits are immediately visible on the host.
- **Source changes** (`src/cli.py`, `src/entrypoint.py`): Visible immediately, take effect on next jail start.
- **Image changes** (`flake.nix`, `src/entrypoint.py`): Require `nix build` + image reload on next `yolo` from the host. The CLI auto-rebuilds when it detects changes.
- **Test changes**: Run `uv run pytest tests/` from inside the jail or on the host.
- **Always commit and push** after changes — the nix image builds from the working tree.

### Testing
- **Host**: `uv run pytest tests/` — all tests run (unit + integration).
- **Inside Jail**: All tests should work. The CLI detects it's inside a container and uses `--userns=host` for nested containers instead of creating new user namespaces.
- **Entrypoint unit tests**: `uv run pytest tests/test_entrypoint.py` — tests config generation (shims, MCP, LSP, bashrc) without containers.

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
- **Auto-Provisioning**: On every jail start, the CLI runs `~/.yolo-bootstrap.sh` with `YOLO_BYPASS_SHIMS=1` (to avoid shim interference) before executing the user's command. The bootstrap script and all config files (MCP, LSP, bashrc, shims) are generated by `src/entrypoint.py` (pure Python, stdlib only). Tools are installed only if missing (idempotent).
  - **NPM Globals**: `chrome-devtools-mcp`, `@modelcontextprotocol/server-sequential-thinking`, `pyright`, `typescript-language-server`, `typescript`
  - **Go Binaries**: `mcp-language-server` (used by Gemini LSP)
  - **Python**: `showboat` (if pip is available)
- **Persistent Storage**: All installed binaries live in `~/.local/share/yolo-jail/home/` on the host, so they survive jail restarts and are reused without reinstalling.
- **Binary Locations**:
    - NPM Globals: `/home/agent/.npm-global/bin/`
    - Go Binaries: `/home/agent/go/bin/`
    - MCP Node Wrappers: `/home/agent/.local/bin/mcp-wrappers/`
- **PATH Order**: `${SHIM_DIR}:/home/agent/.npm-global/bin:/home/agent/go/bin:/mise/shims:/bin:/usr/bin`.

### Agent Package Management

Agents inside the jail can install and manage additional tools via **`mise`**, which persists across jail restarts and isolates tools per workspace.

**Key Concept**: Add tools to your workspace's `mise.toml` file. On next jail start, `mise install` automatically fetches and makes them available.

#### How It Works
1. **Workspace Declaration**: Tools declared in `/workspace/mise.toml` are workspace-specific.
2. **Installation**: At jail startup, `cli.py` runs `mise install` from the workspace, downloading all declared tools into `/mise/installs/`.
3. **Persistence**: Tools are stored in `~/.local/share/yolo-jail/mise/` on the host, surviving jail restarts.
4. **PATH Resolution**: `mise hook-env` resolves tool directories into PATH at startup. Interactive shells also use `mise activate` with PROMPT_COMMAND hooks to keep tools available.

#### Installing a Tool (Example: Typst)

**Step 1**: Add to your workspace's `mise.toml`:
```toml
[tools]
typst = "latest"
```

**Step 2**: On next jail startup (or manually inside jail):
```bash
mise install typst
```

**Step 3**: Use it:
```bash
typst compile myfile.typ output.pdf
```

The tool is now available to the agent and all its subprocesses, and will persist across jail restarts.

#### Available Tools via Mise

Mise supports thousands of tools from registries like **aqua**, **asdf**, and **cargo**. Examples:
- **Build tools**: `typst`, `just`, `protoc`, `cmake`
- **Languages**: `rust`, `zig`, `nim`, `kotlin`
- **CLI tools**: `fd`, `ripgrep`, `bat`, `jq`, `yq`
- **Database**: `postgresql`, `redis`, `sqlite`
- **DevOps**: `terraform`, `ansible`, `kubectl`, `helm`

Search available tools:
```bash
mise registry  # List all available tools
```

#### Workspace vs Global Tools

| Scope | Location | Syntax | Persistence | Visibility |
|-------|----------|--------|-------------|------------|
| **Workspace** | `/workspace/mise.toml` | `[tools] typst = "latest"` | ✅ Survives restarts | ✅ Workspace-specific |
| **Global** | `~/.config/mise/config.toml` | `[tools] typst = "latest"` | ✅ Shared across workspaces | ⚠️ Cross-workspace |

**Recommendation**: Use workspace-level tools in `mise.toml` for project-specific dependencies. This keeps each workspace isolated and reproducible.

#### Troubleshooting

- **Tool not found after installation**: Restart jail or run `eval "$(mise hook-env -s bash)"` in current shell to refresh PATH.
- **Version conflicts**: Each workspace has its own `mise.toml` — edit it to change versions. Multiple versions of same tool can coexist (mise manages them separately).
- **Check installed tools**: `mise ls` (shows all), `mise ls typst` (shows typst versions), `mise which typst` (shows path).
- **Remove a tool**: Delete from `mise.toml` and run `mise uninstall typst@VERSION`, or just leave it — unused tools take no space in PATH.

### Environment Hygiene
- **No Pagers**: Agents cannot handle interactive pagers.
    - `PAGER=cat`, `GIT_PAGER=cat`, `BAT_PAGER=""`.
    - `alias bat='bat --style=plain --paging=never'`.
- **Terminal**: `TERM=xterm-256color` should be passed to maintain color support for agent parsing.
- **Permissions**: Map host UID/GID to the container user to ensure file ownership on the host is preserved.
- **No LD_LIBRARY_PATH Stripping**: `LD_LIBRARY_PATH=/lib:/usr/lib` is baked into the Docker image Env to survive agent environment sanitization.
- **Tmux Window Title**: `cli.py` runs `tmux rename-window JAIL` on the host before exec'ing into the container. This sets the window name to "JAIL" and implicitly disables `automatic-rename` for that window. `PROMPT_COMMAND` inside the jail also emits title escape sequences as a fallback for interactive sessions.
- **Overmind Isolation**: `OVERMIND_SOCKET=/tmp/overmind.sock` is set inside the jail so overmind processes don't conflict with host-side overmind (which defaults to `.overmind.sock` in the workspace directory).
- **Global Gitignore**: The host's global gitignore (`core.excludesFile` or `~/.config/git/ignore`) is mounted read-only and configured via `git config --global core.excludesFile` inside the jail.

## Workflow for Modification
1. **Change Image**: Edit `flake.nix` (e.g., add `pkgs.strace`).
2. **Change Logic**: Edit `src/entrypoint.py` or `src/cli.py`. All Python, no bash heredocs.
3. **Automatic Test**: Run `uv run pytest tests/`.
4. **Manual Test**: Run `yolo -- bash -c "my-new-tool --version"`.
5. **Enforce YOLO**: Always ensure `YOLO_BYPASS_SHIMS=1` is set when running installers inside the jail.
6. **Commit & Push**: Always commit and push after every change. The Nix image is built from the working tree.

## Testing Guidelines
- **Model**: When testing Copilot interactively, always use the `gpt-4.1` model (e.g., `copilot --model gpt-4.1`). Do not use expensive models.
- **No Agent Tests**: Automated tests (`uv run pytest`) must NOT run `copilot` or `gemini` interactively. Tests may check they are installed (`--version`) but must never start interactive sessions or make API calls.
- **Manual Agent Testing**: Always test agent functionality manually before committing. Use `yolo -- copilot --yolo` or `yolo -- gemini --yolo` from a test project.
