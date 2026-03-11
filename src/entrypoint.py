#!/usr/bin/env python3
"""YOLO Jail Container Entrypoint.

Sets up the container environment (shims, configs, prompt) then exec's bash.
Uses only stdlib — runs before any pip packages are installed.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Performance logging
# ---------------------------------------------------------------------------

_PERF_LOG = []
_PERF_START = time.monotonic()


def _perf(label: str):
    """Record a performance checkpoint with elapsed time."""
    elapsed = time.monotonic() - _PERF_START
    _PERF_LOG.append((elapsed, label))


def _perf_dump():
    """Write performance log to ~/.yolo-perf.log for debugging."""
    log_path = HOME / ".yolo-perf.log"
    try:
        prev = None
        lines = [f"=== YOLO Jail Entrypoint Perf ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n"]
        for elapsed, label in _PERF_LOG:
            delta = f"+{elapsed - prev:.3f}s" if prev is not None else "       "
            lines.append(f"  {elapsed:7.3f}s  {delta:>9s}  {label}\n")
            prev = elapsed
        lines.append(f"  Total: {_PERF_LOG[-1][0]:.3f}s\n\n")
        # Append to log (keep last runs visible)
        with open(log_path, "a") as f:
            f.writelines(lines)
        # Trim to last 50 runs
        content = log_path.read_text()
        runs = content.split("=== YOLO")
        if len(runs) > 51:
            log_path.write_text("=== YOLO" + "=== YOLO".join(runs[-50:]))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Paths (from container env vars set by cli.py)
# ---------------------------------------------------------------------------

HOME = Path(os.environ.get("JAIL_HOME", os.environ.get("HOME", "/home/agent")))
SHIM_DIR = HOME / ".yolo-shims"
NPM_PREFIX = Path(os.environ.get("NPM_CONFIG_PREFIX", HOME / ".npm-global"))
GOPATH = Path(os.environ.get("GOPATH", HOME / "go"))
NPM_BIN = NPM_PREFIX / "bin"
GO_BIN = GOPATH / "bin"
MISE_SHIMS = Path(os.environ.get("MISE_DATA_DIR", "/mise")) / "shims"
MCP_WRAPPERS_BIN = HOME / ".local" / "bin" / "mcp-wrappers"
BASHRC_PATH = HOME / ".bashrc"
COPILOT_DIR = HOME / ".copilot"
GEMINI_DIR = HOME / ".gemini"
MISE_CONFIG_DIR = HOME / ".config" / "mise"


# ---------------------------------------------------------------------------
# 1. Generate shims for blocked tools
# ---------------------------------------------------------------------------

def generate_shims():
    """Create shell shims that block or redirect tools per YOLO_BLOCK_CONFIG."""
    if SHIM_DIR.exists():
        shutil.rmtree(SHIM_DIR)
    SHIM_DIR.mkdir(parents=True, exist_ok=True)

    block_json = os.environ.get("YOLO_BLOCK_CONFIG", "")
    if not block_json:
        return

    try:
        config = json.loads(block_json)
    except (json.JSONDecodeError, TypeError):
        return

    for tool_cfg in config:
        name = tool_cfg.get("name")
        if not name:
            continue

        msg = tool_cfg.get("message", f"Error: tool {name} is blocked in this project.")
        sug = tool_cfg.get("suggestion", "")
        real_bin = f"/bin/{name}" if name in ("grep", "find") else None

        lines = ["#!/bin/sh"]
        lines.append('if [ -z "$YOLO_BYPASS_SHIMS" ]; then')
        lines.append(f'  echo "{msg}" >&2')
        if sug:
            lines.append(f'  echo "Suggestion: {sug}" >&2')
        lines.append("  exit 127")
        lines.append("fi")
        if real_bin:
            lines.append(f'exec {real_bin} "$@"')
        lines.append("")

        shim_path = SHIM_DIR / name
        shim_path.write_text("\n".join(lines))
        shim_path.chmod(shim_path.stat().st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# 2. Generate .bashrc
# ---------------------------------------------------------------------------

def generate_bashrc():
    """Write the jail .bashrc with prompt, PATH, aliases, and mise activation."""
    host_dir = os.environ.get("YOLO_HOST_DIR", "unknown")

    content = r"""# YOLO Jail Prompt
YELLOW='\[\033[1;33m\]'
RED='\[\033[1;31m\]'
GREEN='\[\033[1;32m\]'
BLUE='\[\033[1;34m\]'
MAGENTA='\[\033[1;35m\]'
CYAN='\[\033[1;36m\]'
NC='\[\033[0m\]'

JAIL_BANNER="${RED}🔒 YOLO-JAIL${NC}"
HOST_INFO="${CYAN}(host: """ + host_dir + r""")${NC}"

export PS1="\n${JAIL_BANNER} ${HOST_INFO}\n${GREEN}jail${NC}:${BLUE}\w${NC}\$ "

# Set terminal/tmux title (only when inside tmux to avoid literal "JAIL" output)
export PROMPT_COMMAND='[ -n "$TMUX" ] && printf "\033]0;JAIL\033\\"'

# Agent-friendly defaults (no pagers, no line numbers)
export PAGER=cat
export BAT_PAGER=""
export BAT_STYLE="plain"
export GIT_PAGER=cat
# EDITOR=cat prevents agents from getting stuck in interactive editors (e.g. git commit).
# VISUAL=nvim is used by interactive tools like Copilot's ctrl-g (edit prompt in editor).
# Standard Unix convention: programs check VISUAL first for full-screen terminals, EDITOR as fallback.
export EDITOR=cat
export VISUAL=nvim

# PATH with npm-global and go binaries
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
export GOPATH="${GOPATH:-$HOME/go}"
SHIM_DIR="${HOME}/.yolo-shims"
export PATH="$SHIM_DIR:$NPM_CONFIG_PREFIX/bin:$GOPATH/bin:${MISE_DATA_DIR:-/mise}/shims:/bin:/usr/bin"

# Activate mise with shell hooks
eval "$(mise activate bash)"
if [ -f /workspace/mise.toml ]; then
    mise trust /workspace/mise.toml >/dev/null 2>&1 || true
fi

# Aliases
alias ls='ls --color=auto'
alias ll='ls -alF'
alias gemini='gemini --yolo'
alias copilot='copilot --yolo'
alias vi='nvim'
alias vim='nvim'
alias bat='bat --style=plain --paging=never'
"""
    BASHRC_PATH.write_text(content)


# ---------------------------------------------------------------------------
# 3. Bootstrap script (runs after mise is ready)
# ---------------------------------------------------------------------------

def generate_bootstrap_script():
    """Create the idempotent bootstrap script that installs MCP/LSP tools."""
    script_path = HOME / ".yolo-bootstrap.sh"
    script_path.write_text(r"""#!/bin/bash
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
export GOPATH="${GOPATH:-$HOME/go}"
export GOBIN="$GOPATH/bin"
export PATH="$NPM_CONFIG_PREFIX/bin:$GOBIN:$PATH"

# Initialize font cache (once, not on every shell session)
fc-cache -f >/dev/null 2>&1

# Install binaries if missing.
if ! command -v chrome-devtools-mcp >/dev/null; then
    echo "Installing MCP tools via npm..."
    YOLO_BYPASS_SHIMS=1 npm install -g chrome-devtools-mcp @modelcontextprotocol/server-sequential-thinking pyright typescript-language-server typescript
fi

if [ ! -f "$GOBIN/mcp-language-server" ]; then
    if command -v go >/dev/null; then
        echo "Installing mcp-language-server via go..."
        mkdir -p "$GOBIN"
        YOLO_BYPASS_SHIMS=1 go install github.com/isaacphi/mcp-language-server@latest
    else
        echo "Warning: go not found, skipping mcp-language-server install"
    fi
fi

# Install showboat
if ! command -v showboat >/dev/null; then
    echo "Installing showboat..."
    YOLO_BYPASS_SHIMS=1 pip install showboat
fi
""")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# 4. Mise global config
# ---------------------------------------------------------------------------

def generate_mise_config():
    """Write global mise config, injecting tools from YOLO_MISE_TOOLS."""
    config_path = MISE_CONFIG_DIR / "config.toml"

    # Parse injected tools from env (set by cli.py from yolo-jail.jsonc)
    import json as _json
    try:
        injected_tools = _json.loads(os.environ.get("YOLO_MISE_TOOLS", "{}"))
    except (ValueError, TypeError):
        injected_tools = {}

    # Base tools always present in the jail
    base_tools = {
        "node": "22",
        "python": "3.13",
        "go": "latest",
        '"npm:@google/gemini-cli"': "latest",
        '"npm:@github/copilot"': "latest",
    }

    if not config_path.exists():
        MISE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = ["[tools]"]
        for tool, version in base_tools.items():
            lines.append(f'{tool} = "{version}"')
        for tool, version in injected_tools.items():
            lines.append(f'{tool} = "{version}"')
        config_path.write_text("\n".join(lines) + "\n")
        return

    # Update existing config: add/update only injected tools
    if injected_tools:
        import re
        content = config_path.read_text()
        for tool, version in injected_tools.items():
            pattern = rf'^{re.escape(tool)}\s*=\s*"[^"]*"'
            if re.search(pattern, content, re.MULTILINE):
                content = re.sub(pattern, f'{tool} = "{version}"', content, flags=re.MULTILINE)
            else:
                content = content.rstrip("\n") + f'\n{tool} = "{version}"\n'
        config_path.write_text(content)


# ---------------------------------------------------------------------------
# 5. MCP wrappers (node, npx, chrome)
# ---------------------------------------------------------------------------

def _write_executable(path: Path, content: str):
    """Write content to path and make executable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def generate_mcp_wrappers():
    """Create wrapper scripts for node, npx, and chrome-devtools-mcp."""
    # Chrome wrapper
    _write_executable(HOME / ".local" / "bin" / "chrome-devtools-mcp-wrapper", r"""#!/bin/bash
# Self-contained wrapper: sets its own env since agents sanitize child processes.
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"

# Internal Chrome debugging defaults (isolated to container)
CHROME_PORT="${CHROME_DEBUG_PORT:-9222}"
CHROME_ADDR="${CHROME_DEBUG_ADDR:-127.0.0.1}"
CHROME_URL="http://$CHROME_ADDR:$CHROME_PORT"

NPM_BIN="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}/bin"
MCP_WRAPPERS_BIN="$HOME/.local/bin/mcp-wrappers"

# Start Chromium if not already running
if ! curl -s "$CHROME_URL/json/version" >/dev/null 2>&1; then
    /usr/bin/chromium \
        --headless=new \
        --no-sandbox \
        --disable-dev-shm-usage \
        --disable-setuid-sandbox \
        --disable-gpu \
        --disable-software-rasterizer \
        --disable-blink-features=AutomationControlled \
        --disable-breakpad \
        --noerrdialogs \
        --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36" \
        --remote-debugging-address=$CHROME_ADDR \
        --remote-debugging-port=$CHROME_PORT \
        &>/dev/null &

    # Wait for Chrome to be ready
    for i in $(seq 1 30); do
        if curl -s "$CHROME_URL/json/version" >/dev/null 2>&1; then
            break
        fi
        sleep 0.2
    done
fi

exec "$MCP_WRAPPERS_BIN/node" "$NPM_BIN/chrome-devtools-mcp" \
    --browser-url "$CHROME_URL" \
    "$@"
""")

    # Node wrapper — bypass mise shims to avoid workspace env overhead on MCP startup
    _write_executable(MCP_WRAPPERS_BIN / "node", r"""#!/bin/bash
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
exec /bin/node "$@"
""")

    # npx wrapper — bypass mise shims for same reason
    _write_executable(MCP_WRAPPERS_BIN / "npx", r"""#!/bin/bash
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
exec /bin/npx "$@"
""")


# ---------------------------------------------------------------------------
# 6. Git config
# ---------------------------------------------------------------------------

def configure_git():
    """Set git name, email, and global gitignore from host env vars."""
    if not shutil.which("git"):
        return
    env = os.environ
    if env.get("YOLO_GIT_NAME"):
        subprocess.run(["git", "config", "--global", "user.name", env["YOLO_GIT_NAME"]],
                       capture_output=True)
    if env.get("YOLO_GIT_EMAIL"):
        subprocess.run(["git", "config", "--global", "user.email", env["YOLO_GIT_EMAIL"]],
                       capture_output=True)
    gitignore = env.get("YOLO_GLOBAL_GITIGNORE", "")
    if gitignore and Path(gitignore).is_file():
        subprocess.run(["git", "config", "--global", "core.excludesFile", gitignore],
                       capture_output=True)


def configure_jj():
    """Set jj user identity from host env vars."""
    if not shutil.which("jj"):
        return
    env = os.environ
    if env.get("YOLO_JJ_NAME"):
        subprocess.run(["jj", "config", "set", "--user", "user.name", env["YOLO_JJ_NAME"]],
                       capture_output=True)
    if env.get("YOLO_JJ_EMAIL"):
        subprocess.run(["jj", "config", "set", "--user", "user.email", env["YOLO_JJ_EMAIL"]],
                       capture_output=True)


# ---------------------------------------------------------------------------
# 7. Skills directory merging
# ---------------------------------------------------------------------------

def merge_skills():
    """Sync host + workspace skills into Copilot and Gemini skills dirs (read-only)."""
    host_skills_path = os.environ.get("YOLO_HOST_GEMINI_SKILLS", "")

    for agent_dir in [COPILOT_DIR, GEMINI_DIR]:
        jail_skills = agent_dir / "skills"
        if jail_skills.exists():
            # Restore write permission before rmtree (we chmod -w on previous runs)
            _make_writable(jail_skills)
            shutil.rmtree(jail_skills)
        jail_skills.mkdir(parents=True, exist_ok=True)

        # Host user-level skills
        if host_skills_path:
            _copy_skill_dirs(Path(host_skills_path), jail_skills)

        # Workspace skills (take precedence) — check both .copilot and .gemini
        for ws_dir in ["/workspace/.copilot/skills", "/workspace/.gemini/skills"]:
            ws_skills = Path(ws_dir)
            if ws_skills.is_dir():
                _copy_skill_dirs(ws_skills, jail_skills)

        # Make skills read-only so agents can't modify them
        _make_readonly(jail_skills)


def _copy_skill_dirs(src: Path, dst: Path):
    """Copy skill subdirectories from src to dst, following symlinks."""
    if not src.is_dir():
        return
    import stat
    for item in src.iterdir():
        if item.is_dir():
            target = dst / item.name
            if target.exists():
                # Restore write permissions (may have been made read-only)
                dst.chmod(dst.stat().st_mode | stat.S_IWUSR)
                _make_writable(target)
                shutil.rmtree(target)
            shutil.copytree(item, target, symlinks=False)


def _make_readonly(path: Path):
    """Recursively remove write permission from a directory tree."""
    import stat
    for root, dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            fp.chmod(fp.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        for d in dirs:
            dp = Path(root) / d
            dp.chmod(dp.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _make_writable(path: Path):
    """Recursively restore write permission on a directory tree."""
    import stat
    path.chmod(path.stat().st_mode | stat.S_IWUSR)
    for root, dirs, files in os.walk(path):
        for d in dirs:
            dp = Path(root) / d
            dp.chmod(dp.stat().st_mode | stat.S_IWUSR)
        for f in files:
            fp = Path(root) / f
            fp.chmod(fp.stat().st_mode | stat.S_IWUSR)


# ---------------------------------------------------------------------------
# 8. Copilot config (MCP + LSP)
# ---------------------------------------------------------------------------

def _chrome_devtools_args() -> list:
    """Common chrome-devtools-mcp args."""
    return [
        str(NPM_BIN / "chrome-devtools-mcp"),
        "--headless", "--isolated",
        "--executablePath", "/usr/bin/chromium",
        "--chrome-arg=--no-sandbox",
        "--chrome-arg=--disable-dev-shm-usage",
        "--chrome-arg=--disable-setuid-sandbox",
        "--chrome-arg=--disable-gpu",
        "--chrome-arg=--disable-software-rasterizer",
    ]


def configure_copilot():
    """Set up Copilot directory, MCP config, and LSP config."""
    COPILOT_DIR.mkdir(parents=True, exist_ok=True)

    config_json = COPILOT_DIR / "config.json"
    if not config_json.exists():
        config_json.write_text('{"yolo": true}\n')

    # MCP config
    mcp_config = {
        "mcpServers": {
            "chrome-devtools": {
                "command": str(MCP_WRAPPERS_BIN / "node"),
                "args": _chrome_devtools_args(),
            },
            "sequential-thinking": {
                "command": str(MCP_WRAPPERS_BIN / "node"),
                "args": [str(NPM_BIN / "mcp-server-sequential-thinking")],
            },
        }
    }
    (COPILOT_DIR / "mcp-config.json").write_text(json.dumps(mcp_config, indent=2) + "\n")

    # LSP config
    lsp_config = {
        "lspServers": {
            "python": {
                "command": str(NPM_BIN / "pyright-langserver"),
                "args": ["--stdio"],
                "fileExtensions": {".py": "python", ".pyi": "python"},
            },
            "typescript": {
                "command": str(NPM_BIN / "typescript-language-server"),
                "args": ["--stdio"],
                "fileExtensions": {
                    ".ts": "typescript", ".tsx": "typescriptreact",
                    ".js": "javascript", ".jsx": "javascriptreact",
                },
            },
        }
    }
    (COPILOT_DIR / "lsp-config.json").write_text(json.dumps(lsp_config, indent=2) + "\n")


# ---------------------------------------------------------------------------
# 9. Gemini config (MCP + LSP in settings.json)
# ---------------------------------------------------------------------------

def configure_gemini():
    """Set up Gemini settings with MCP servers, merging with existing config."""
    GEMINI_DIR.mkdir(parents=True, exist_ok=True)
    config_path = GEMINI_DIR / "settings.json"

    default_servers = {
        "chrome-devtools": {
            "command": str(MCP_WRAPPERS_BIN / "node"),
            "args": _chrome_devtools_args(),
        },
        "sequential-thinking": {
            "command": str(MCP_WRAPPERS_BIN / "node"),
            "args": [str(NPM_BIN / "mcp-server-sequential-thinking")],
        },
        "python-lsp": {
            "command": str(GO_BIN / "mcp-language-server"),
            "args": ["-lsp", "pyright-langserver", "-workspace", "/workspace", "--", "--stdio"],
        },
        "typescript-lsp": {
            "command": str(GO_BIN / "mcp-language-server"),
            "args": ["-lsp", "typescript-language-server", "-workspace", "/workspace", "--", "--stdio"],
        },
    }

    try:
        if config_path.exists():
            try:
                current = json.loads(config_path.read_text())
            except json.JSONDecodeError:
                current = {}
        else:
            current = {}

        current.setdefault("mcpServers", {}).update(default_servers)
        current.setdefault("security", {})
        current["security"].setdefault("approvalMode", "yolo")
        current["security"].setdefault("enablePermanentToolApproval", True)

        config_path.write_text(json.dumps(current, indent=2) + "\n")
    except Exception as e:
        print(f"Error configuring Gemini MCP: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 10. Finalize PATH and exec bash
# ---------------------------------------------------------------------------

def exec_bash(command: str):
    """Set up final PATH, activate mise, and exec bash with the given command."""
    path = f"{SHIM_DIR}:{NPM_BIN}:{GO_BIN}:{MISE_SHIMS}:/bin:/usr/bin"
    os.environ["PATH"] = path

    os.execvp("bash", [
        "bash", "--rcfile", str(BASHRC_PATH), "-c", command,
    ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "bash"
    _perf("start")

    # Create /mise symlink for backward compat when MISE_DATA_DIR is the host path.
    # Scripts and PATH entries may reference /mise/shims — this ensures they resolve.
    mise_data = os.environ.get("MISE_DATA_DIR", "/mise")
    if mise_data != "/mise" and not Path("/mise").exists():
        try:
            Path("/mise").symlink_to(mise_data)
        except OSError:
            pass  # may lack permissions on /

    generate_shims()
    _perf("generate_shims")
    generate_bashrc()
    _perf("generate_bashrc")
    generate_bootstrap_script()
    _perf("generate_bootstrap_script")
    generate_mise_config()
    _perf("generate_mise_config")
    generate_mcp_wrappers()
    _perf("generate_mcp_wrappers")
    configure_git()
    _perf("configure_git")
    configure_jj()
    _perf("configure_jj")
    merge_skills()
    _perf("merge_skills")
    configure_copilot()
    _perf("configure_copilot")
    configure_gemini()
    _perf("configure_gemini")

    # Set PATH including mise shims so tools like copilot/gemini are found
    os.environ["PATH"] = f"{SHIM_DIR}:{NPM_BIN}:{GO_BIN}:{MISE_SHIMS}:/bin:/usr/bin"

    # Activate mise for the current process so hook-env works
    try:
        result = subprocess.run(
            ["mise", "activate", "bash"], capture_output=True, text=True
        )
        if result.returncode == 0:
            # We can't eval bash in Python, but we set PATH for the exec'd shell
            pass
    except FileNotFoundError:
        pass
    _perf("mise_activate")

    # Trust workspace mise.toml
    if Path("/workspace/mise.toml").exists():
        subprocess.run(["mise", "trust", "/workspace/mise.toml"],
                       capture_output=True)

    # Apply mise hook-env for non-interactive shells
    try:
        result = subprocess.run(
            ["mise", "hook-env", "-s", "bash"], capture_output=True, text=True
        )
    except FileNotFoundError:
        pass
    _perf("mise_hook_env")

    _perf_dump()
    exec_bash(cmd)


if __name__ == "__main__":
    main()
