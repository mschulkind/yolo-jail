#!/usr/bin/env python3
"""YOLO Jail Container Entrypoint.

Sets up the container environment (shims, configs, prompt) then exec's bash.
Uses only stdlib — runs before any pip packages are installed.
"""

import hashlib
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
        lines = [
            f"=== YOLO Jail Entrypoint Perf ({time.strftime('%Y-%m-%d %H:%M:%S')}) ===\n"
        ]
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
GEMINI_MANAGED_MCP_PATH = GEMINI_DIR / "yolo-managed-mcp-servers.json"
CLAUDE_DIR = HOME / ".claude"
CLAUDE_MANAGED_MCP_PATH = CLAUDE_DIR / "yolo-managed-mcp-servers.json"
MISE_CONFIG_DIR = HOME / ".config" / "mise"

# Default LSP servers always available in the jail.
# command: absolute path (for Copilot); basename extracted for Gemini's mcp-language-server.
# args: passed to the LSP binary directly.
# fileExtensions: extension → language ID map (required for Copilot).
DEFAULT_LSP_SERVERS = {
    "python": {
        "command": str(NPM_BIN / "pyright-langserver"),
        "args": ["--stdio"],
        "fileExtensions": {".py": "python", ".pyi": "python"},
    },
    "typescript": {
        "command": str(NPM_BIN / "typescript-language-server"),
        "args": ["--stdio"],
        "fileExtensions": {
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
        },
    },
    "go": {
        "command": str(GO_BIN / "gopls"),
        "args": [],
        "fileExtensions": {".go": "go"},
    },
}


def _load_lsp_servers():
    """Load LSP server config: defaults merged with workspace overrides from YOLO_LSP_SERVERS."""
    servers = dict(DEFAULT_LSP_SERVERS)
    extra_json = os.environ.get("YOLO_LSP_SERVERS", "")
    if extra_json:
        try:
            extra = json.loads(extra_json)
            if isinstance(extra, dict):
                servers.update(extra)
        except (json.JSONDecodeError, TypeError):
            pass
    return servers


# ---------------------------------------------------------------------------
# 1. Generate shims for blocked tools
# ---------------------------------------------------------------------------


def generate_shims():
    """Create shell shims that block or redirect tools per YOLO_BLOCK_CONFIG."""
    # Use ignore_errors to handle races when multiple jails start concurrently
    # and both try to rmtree the same shared home directory.
    shutil.rmtree(SHIM_DIR, ignore_errors=True)
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


def generate_agent_launchers():
    """Create lazy-update wrappers for agent CLIs (gemini, copilot, claude).

    Instead of updating all agents at boot (slow), these wrappers update the
    specific agent on first use.  They sit in SHIM_DIR (highest PATH priority)
    and self-delete after ensuring the real binary is current.
    """
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    stamp_dir = HOME / ".cache" / "yolo-agent-stamps"

    # npm-based agents: gemini, copilot
    npm_agents = {
        "gemini": "@google/gemini-cli",
        "copilot": "@github/copilot",
    }
    for bin_name, pkg_name in npm_agents.items():
        # Don't overwrite a blocked-tool shim
        shim_path = SHIM_DIR / bin_name
        if shim_path.exists():
            continue

        launcher = f"""#!/bin/bash
# Lazy-update launcher for {bin_name} — updates on first use, not at boot.
set -euo pipefail
export NPM_CONFIG_PREFIX="${{NPM_CONFIG_PREFIX:-$HOME/.npm-global}}"
export NPM_CONFIG_CACHE="${{NPM_CONFIG_CACHE:-$HOME/.cache/npm}}"
STAMP_DIR="{stamp_dir}"
STAMP="$STAMP_DIR/{bin_name}.stamp"
REAL_BIN="$NPM_CONFIG_PREFIX/bin/{bin_name}"
PKG="{pkg_name}"
UPDATE_INTERVAL=3600  # seconds between update checks

mkdir -p "$STAMP_DIR"

_do_install() {{
    echo "  Installing $PKG..." >&2
    # Clean stale npm temp dirs that cause ENOTEMPTY
    rm -rf "$NPM_CONFIG_PREFIX"/lib/node_modules/${{PKG%%/*}}/.${{PKG##*/}}-* 2>/dev/null
    YOLO_BYPASS_SHIMS=1 npm install -g --prefer-online "$PKG@latest" 2>&1 || true
    touch "$STAMP"
}}

if [ ! -x "$REAL_BIN" ]; then
    _do_install
elif [ ! -f "$STAMP" ]; then
    # First run since jail boot — check if update needed
    INSTALLED=$(jq -r '.version' "$NPM_CONFIG_PREFIX/lib/node_modules/$PKG/package.json" 2>/dev/null || echo "0")
    LATEST=$(YOLO_BYPASS_SHIMS=1 npm view "$PKG" version 2>/dev/null || echo "$INSTALLED")
    if [ "$INSTALLED" != "$LATEST" ]; then
        echo "  Updating {bin_name} $INSTALLED → $LATEST..." >&2
        _do_install
    else
        touch "$STAMP"
    fi
else
    # Check if stamp is stale (older than UPDATE_INTERVAL)
    STAMP_AGE=$(( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ))
    if [ "$STAMP_AGE" -gt "$UPDATE_INTERVAL" ]; then
        INSTALLED=$(jq -r '.version' "$NPM_CONFIG_PREFIX/lib/node_modules/$PKG/package.json" 2>/dev/null || echo "0")
        LATEST=$(YOLO_BYPASS_SHIMS=1 npm view "$PKG" version 2>/dev/null || echo "$INSTALLED")
        if [ "$INSTALLED" != "$LATEST" ]; then
            echo "  Updating {bin_name} $INSTALLED → $LATEST..." >&2
            _do_install
        else
            touch "$STAMP"
        fi
    fi
fi

if [ -x "$REAL_BIN" ]; then
    exec "$REAL_BIN" "$@"
else
    echo "  ⚠ {bin_name} not available" >&2
    exit 1
fi
"""
        shim_path.write_text(launcher)
        shim_path.chmod(shim_path.stat().st_mode | stat.S_IEXEC)

    # Claude: native installer
    claude_shim = SHIM_DIR / "claude"
    if not claude_shim.exists():
        launcher = f"""#!/bin/bash
# Lazy-update launcher for claude — installs/updates on first use, not at boot.
set -euo pipefail
STAMP_DIR="{stamp_dir}"
STAMP="$STAMP_DIR/claude.stamp"
REAL_BIN="$HOME/.local/bin/claude"
UPDATE_INTERVAL=3600

mkdir -p "$STAMP_DIR"

_do_install() {{
    echo "  Installing Claude Code..." >&2
    YOLO_BYPASS_SHIMS=1 curl -fsSL https://claude.ai/install.sh | bash 2>&1 || true
    touch "$STAMP"
}}

if [ ! -x "$REAL_BIN" ]; then
    _do_install
elif [ ! -f "$STAMP" ]; then
    # First run since boot — try a quick update
    YOLO_BYPASS_SHIMS=1 "$REAL_BIN" install 2>&1 || true
    touch "$STAMP"
else
    STAMP_AGE=$(( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ))
    if [ "$STAMP_AGE" -gt "$UPDATE_INTERVAL" ]; then
        YOLO_BYPASS_SHIMS=1 "$REAL_BIN" install 2>&1 || true
        touch "$STAMP"
    fi
fi

if [ -x "$REAL_BIN" ]; then
    exec "$REAL_BIN" "$@"
else
    echo "  ⚠ claude not available" >&2
    exit 1
fi
"""
        claude_shim.write_text(launcher)
        claude_shim.chmod(claude_shim.stat().st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# 2. Generate .bashrc
# ---------------------------------------------------------------------------


def generate_bashrc():
    """Write the jail .bashrc with prompt, PATH, aliases, and mise activation."""
    host_dir = os.environ.get("YOLO_HOST_DIR", "unknown")

    content = (
        r"""# YOLO Jail Prompt
YELLOW='\[\033[1;33m\]'
RED='\[\033[1;31m\]'
GREEN='\[\033[1;32m\]'
BLUE='\[\033[1;34m\]'
MAGENTA='\[\033[1;35m\]'
CYAN='\[\033[1;36m\]'
NC='\[\033[0m\]'

JAIL_BANNER="${RED}🔒 YOLO-JAIL${NC}"
HOST_INFO="${CYAN}(host: """
        + host_dir
        + r""")${NC}"

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
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$HOME/.cache/npm}"
export GOPATH="${GOPATH:-$HOME/go}"
SHIM_DIR="${HOME}/.yolo-shims"
export PATH="$SHIM_DIR:$HOME/.local/bin:$NPM_CONFIG_PREFIX/bin:${MISE_DATA_DIR:-/mise}/shims:$GOPATH/bin:/bin:/usr/bin"

# Activate mise with shell hooks (interactive shells only).
# Non-interactive shells (bash -lc) skip activation to avoid a deadlock:
# mise hook-env holds a lock then spawns uv via the mise shim (which IS mise),
# re-entering mise locking. The caller's eval "$(mise env ...)" already set up
# the environment before spawning this shell.
if [[ $- == *i* ]]; then
    eval "$(mise activate bash)"
fi
if [ -f /workspace/mise.toml ]; then
    mise trust --quiet /workspace/mise.toml 2>/dev/null || true
fi

# Aliases
alias ls='ls --color=auto'
alias ll='ls -alF'
alias gemini='gemini --yolo'
alias copilot='copilot --yolo --no-auto-update'
# Claude YOLO mode: cli.py injects --dangerously-skip-permissions (with
# IS_SANDBOX=1 to bypass the root check) + settings.json permissions.allow rules.
alias vi='nvim'
alias vim='nvim'
alias bat='bat --style=plain --paging=never'
"""
    )
    BASHRC_PATH.write_text(content)


# ---------------------------------------------------------------------------
# 3. Bootstrap script (runs after mise is ready)
# ---------------------------------------------------------------------------


def generate_bootstrap_script():
    """Create the idempotent bootstrap script that installs MCP/LSP tools."""
    script_path = HOME / ".yolo-bootstrap.sh"
    script_path.write_text(r"""#!/bin/bash
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$HOME/.cache/npm}"
export GOPATH="${GOPATH:-$HOME/go}"
export GOBIN="$GOPATH/bin"
export PATH="$HOME/.local/bin:$NPM_CONFIG_PREFIX/bin:${MISE_DATA_DIR:-/mise}/shims:$GOBIN:$PATH"

# Initialize font cache (once, not on every shell session)
fc-cache -f >/dev/null 2>&1

# Agent CLIs (gemini, copilot, claude) are NOT updated here.
# Lazy-update launchers in ~/.yolo-shims/ handle install/update on first use,
# keeping boot fast.  Only MCP/LSP tools that agents depend on are installed here.

# Install binaries if missing.
if ! command -v chrome-devtools-mcp >/dev/null; then
    echo "  Installing MCP tools..." >&2
    YOLO_BYPASS_SHIMS=1 npm install -g chrome-devtools-mcp @modelcontextprotocol/server-sequential-thinking pyright typescript-language-server typescript
fi

if [ ! -f "$GOBIN/mcp-language-server" ] || [ ! -f "$GOBIN/gopls" ]; then
    if command -v go >/dev/null; then
        echo "  Installing Go tools..." >&2
        mkdir -p "$GOBIN"
        [ -f "$GOBIN/mcp-language-server" ] || YOLO_BYPASS_SHIMS=1 go install github.com/isaacphi/mcp-language-server@latest
        [ -f "$GOBIN/gopls" ] || YOLO_BYPASS_SHIMS=1 go install golang.org/x/tools/gopls@latest
    else
        echo "  ⚠ go not found, skipping Go tool installs" >&2
    fi
fi

# Install showboat
if ! command -v showboat >/dev/null; then
    echo "  Installing showboat..." >&2
    YOLO_BYPASS_SHIMS=1 pip install showboat
fi
""")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


def generate_venv_precreate_script():
    """Create a script that pre-creates python venvs using real binaries.

    Must run AFTER `mise install` (so tools are available) and BEFORE
    `mise hook-env` / `mise env` (which would deadlock trying to create
    venvs via the mise shim).
    """
    script_path = HOME / ".yolo-venv-precreate.sh"
    script_path.write_text(r"""#!/bin/bash
# Pre-create python venvs to avoid a mise shim deadlock.
# When _.python.venv={create:true} is configured, mise hook-env spawns
# uv via the mise shim (which IS /bin/mise), re-entering mise's flock
# and deadlocking.  Creating the venv beforehand with the real uv binary
# means mise finds it already exists and skips the uv call.

[ -f /workspace/mise.toml ] || exit 0

# Get real binary paths (not shims) — requires mise install to have run
_uv=$(mise which uv 2>/dev/null) || exit 0
_py=$(mise which python 2>/dev/null) || exit 0
[ -n "$_uv" ] && [ -n "$_py" ] || exit 0

# Parse venv path from mise.toml
_vp=$(/bin/python3 -c "
import tomllib, sys
try:
    c = tomllib.load(open('/workspace/mise.toml', 'rb'))
    v = c.get('env', {}).get('_.python.venv', {})
    if isinstance(v, dict):
        if v.get('create', False):
            print(v.get('path', '.venv'))
        else:
            sys.exit(1)
    elif isinstance(v, str):
        print(v)
    else:
        sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null) || exit 0

[ -d "/workspace/$_vp" ] && exit 0
"$_uv" venv "/workspace/$_vp" --python "$_py" 2>/dev/null || true
""")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# 4. Mise global config
# ---------------------------------------------------------------------------


def _toml_key(key: str) -> str:
    """Quote a TOML key if it contains characters that aren't valid in bare keys."""
    import re as _re

    if _re.fullmatch(r"[A-Za-z0-9_-]+", key):
        return key
    return f'"{key}"'


def generate_mise_config():
    """Write global mise config, injecting tools from YOLO_MISE_TOOLS."""
    config_path = MISE_CONFIG_DIR / "config.toml"

    # Parse injected tools from env (set by cli.py from yolo-jail.jsonc)
    import json as _json

    try:
        injected_tools = _json.loads(os.environ.get("YOLO_MISE_TOOLS", "{}"))
    except (ValueError, TypeError):
        injected_tools = {}

    # Base tools always present in the jail.
    # NOTE: copilot, gemini, and claude are NOT managed by mise — the bootstrap
    # script handles their installation (npm install -g for copilot/gemini,
    # native installer for claude) to avoid mise's version cache preventing
    # updates and the npm deprecation warning for claude.
    base_tools = {
        "node": "22",
        "python": "3.13",
        "go": "latest",
    }

    # Tools that used to be in base_tools but are now bootstrap-managed.
    # Remove from existing configs to avoid stale mise-cached versions
    # shadowing the always-fresh npm global installs.
    retired_tools = [
        '"npm:@github/copilot"',
        "gemini",
        '"npm:@anthropic-ai/claude-code"',
    ]

    if not config_path.exists():
        MISE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = ["[tools]"]
        for tool, version in base_tools.items():
            lines.append(f'{_toml_key(tool)} = "{version}"')
        for tool, version in injected_tools.items():
            lines.append(f'{_toml_key(tool)} = "{version}"')
        config_path.write_text("\n".join(lines) + "\n")
        return

    # Update existing config:
    # - base_tools: add if missing (don't overwrite user customizations)
    # - injected_tools: always add or update (explicit overrides from config)
    # - retired_tools: remove if present (moved to bootstrap npm install)
    import re

    content = config_path.read_text()
    changed = False

    # Remove retired tools (now managed by bootstrap npm install, not mise)
    for tool in retired_tools:
        pattern = rf'^{re.escape(tool)}\s*=\s*"[^"]*"\n?'
        new_content = re.sub(pattern, "", content, flags=re.MULTILINE)
        if new_content != content:
            content = new_content
            changed = True

    # Ensure all base tools are present and not using deprecated "system" value.
    # mise deprecated @system — replace with the base version.
    for tool, version in base_tools.items():
        tk = _toml_key(tool)
        pattern = rf'^"?{re.escape(tool)}"?\s*=\s*"[^"]*"'
        match = re.search(pattern, content, re.MULTILINE)
        if not match:
            content = content.rstrip("\n") + f'\n{tk} = "{version}"\n'
            changed = True
        elif '"system"' in match.group():
            content = (
                content[: match.start()]
                + f'{tk} = "{version}"'
                + content[match.end() :]
            )
            changed = True

    # Injected tools always override
    for tool, version in injected_tools.items():
        tk = _toml_key(tool)
        pattern = rf'^"?{re.escape(tool)}"?\s*=\s*"[^"]*"'
        if re.search(pattern, content, re.MULTILINE):
            new_content = re.sub(
                pattern, f'{tk} = "{version}"', content, flags=re.MULTILINE
            )
            if new_content != content:
                content = new_content
                changed = True
        else:
            content = content.rstrip("\n") + f'\n{tk} = "{version}"\n'
            changed = True

    if changed:
        config_path.write_text(content)

    # Also retire from workspace mise.toml if present (mounted from host).
    ws_mise = Path("/workspace/mise.toml")
    if ws_mise.exists():
        ws_content = ws_mise.read_text()
        ws_changed = False
        for tool in retired_tools:
            pattern = rf'^{re.escape(tool)}\s*=\s*"[^"]*"\n?'
            new_ws = re.sub(pattern, "", ws_content, flags=re.MULTILINE)
            if new_ws != ws_content:
                ws_content = new_ws
                ws_changed = True
        if ws_changed:
            ws_mise.write_text(ws_content)

    # Uninstall retired mise tools so stale binaries don't shadow bootstrap ones.
    # mise uninstall is idempotent — safe to call even if already removed.
    for tool in retired_tools:
        tool_name = tool.strip('"')
        try:
            subprocess.run(
                ["mise", "uninstall", "--all", tool_name],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass


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
    _write_executable(
        HOME / ".local" / "bin" / "chrome-devtools-mcp-wrapper",
        r"""#!/bin/bash
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
""",
    )

    # Node wrapper — bypass mise shims to avoid workspace env overhead on MCP startup
    _write_executable(
        MCP_WRAPPERS_BIN / "node",
        r"""#!/bin/bash
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
exec /bin/node "$@"
""",
    )

    # npx wrapper — bypass mise shims for same reason
    _write_executable(
        MCP_WRAPPERS_BIN / "npx",
        r"""#!/bin/bash
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
exec /bin/npx "$@"
""",
    )


# ---------------------------------------------------------------------------
# 6. Git config
# ---------------------------------------------------------------------------


def configure_git():
    """Set git name, email, and global gitignore from host env vars."""
    if not shutil.which("git"):
        return
    env = os.environ
    if env.get("YOLO_GIT_NAME"):
        subprocess.run(
            ["git", "config", "--global", "user.name", env["YOLO_GIT_NAME"]],
            capture_output=True,
        )
    if env.get("YOLO_GIT_EMAIL"):
        subprocess.run(
            ["git", "config", "--global", "user.email", env["YOLO_GIT_EMAIL"]],
            capture_output=True,
        )
    gitignore = env.get("YOLO_GLOBAL_GITIGNORE", "")
    if gitignore and Path(gitignore).is_file():
        subprocess.run(
            ["git", "config", "--global", "core.excludesFile", gitignore],
            capture_output=True,
        )


def configure_jj():
    """Set jj user identity from host env vars."""
    if not shutil.which("jj"):
        return
    env = os.environ
    if env.get("YOLO_JJ_NAME"):
        subprocess.run(
            ["jj", "config", "set", "--user", "user.name", env["YOLO_JJ_NAME"]],
            capture_output=True,
        )
    if env.get("YOLO_JJ_EMAIL"):
        subprocess.run(
            ["jj", "config", "set", "--user", "user.email", env["YOLO_JJ_EMAIL"]],
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# 7. Skills directory merging
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 8. Copilot config (MCP + LSP)
# ---------------------------------------------------------------------------


def _chrome_devtools_args() -> list:
    """Common chrome-devtools-mcp args."""
    return [
        str(NPM_BIN / "chrome-devtools-mcp"),
        "--headless",
        "--isolated",
        "--executablePath",
        "/usr/bin/chromium",
        "--chrome-arg=--no-sandbox",
        "--chrome-arg=--disable-dev-shm-usage",
        "--chrome-arg=--disable-setuid-sandbox",
        "--chrome-arg=--disable-gpu",
        "--chrome-arg=--disable-software-rasterizer",
    ]


def _load_mcp_servers():
    """Load MCP servers from presets plus YOLO_MCP_SERVERS overrides.

    Presets are expanded from YOLO_MCP_PRESETS (JSON array of preset names).
    Custom servers from YOLO_MCP_SERVERS are merged on top.
    A null value removes a preset or inherited server.
    """
    presets = {
        "chrome-devtools": {
            "command": str(MCP_WRAPPERS_BIN / "node"),
            "args": _chrome_devtools_args(),
        },
        "sequential-thinking": {
            "command": str(MCP_WRAPPERS_BIN / "node"),
            "args": [str(NPM_BIN / "mcp-server-sequential-thinking")],
        },
    }

    # Start empty — presets are opt-in
    servers = {}

    # Expand requested presets
    presets_json = os.environ.get("YOLO_MCP_PRESETS", "")
    if presets_json:
        try:
            preset_names = json.loads(presets_json)
            if isinstance(preset_names, list):
                for name in preset_names:
                    if isinstance(name, str) and name in presets:
                        servers[name] = presets[name]
        except (json.JSONDecodeError, TypeError):
            pass

    # Merge custom servers (overrides, additions, and null-removals)
    extra_json = os.environ.get("YOLO_MCP_SERVERS", "")
    if extra_json:
        try:
            extra = json.loads(extra_json)
            if isinstance(extra, dict):
                for name, cfg in extra.items():
                    if cfg is None:
                        servers.pop(name, None)
                    elif isinstance(cfg, dict):
                        servers[name] = cfg
        except (json.JSONDecodeError, TypeError):
            pass
    return servers


def configure_copilot():
    """Set up Copilot directory, MCP config, and LSP config."""
    COPILOT_DIR.mkdir(parents=True, exist_ok=True)

    config_json = COPILOT_DIR / "config.json"
    if not config_json.exists():
        config_json.write_text('{"yolo": true}\n')

    # MCP config
    mcp_config = {"mcpServers": _load_mcp_servers()}
    (COPILOT_DIR / "mcp-config.json").write_text(
        json.dumps(mcp_config, indent=2) + "\n"
    )

    # LSP config (defaults + workspace overrides from YOLO_LSP_SERVERS)
    servers = _load_lsp_servers()
    lsp_config = {"lspServers": {}}
    for name, cfg in servers.items():
        lsp_config["lspServers"][name] = {
            "command": cfg["command"],
            "args": cfg.get("args", []),
            "fileExtensions": cfg.get("fileExtensions", {}),
        }
    (COPILOT_DIR / "lsp-config.json").write_text(
        json.dumps(lsp_config, indent=2) + "\n"
    )


# ---------------------------------------------------------------------------
# 9. Gemini config (MCP + LSP in settings.json)
# ---------------------------------------------------------------------------


def configure_gemini():
    """Set up Gemini settings with MCP servers, merging with existing config."""
    GEMINI_DIR.mkdir(parents=True, exist_ok=True)
    config_path = GEMINI_DIR / "settings.json"

    configured_servers = _load_mcp_servers()

    # Add LSP servers wrapped as MCP via mcp-language-server
    lsp_servers = _load_lsp_servers()
    for name, cfg in lsp_servers.items():
        cmd = cfg["command"]
        bare_cmd = Path(cmd).name
        lsp_args = cfg.get("args", [])
        mcp_args = ["-lsp", bare_cmd, "-workspace", "/workspace"]
        if lsp_args:
            mcp_args.extend(["--"] + lsp_args)
        configured_servers[f"{name}-lsp"] = {
            "command": str(GO_BIN / "mcp-language-server"),
            "args": mcp_args,
        }

    try:
        if config_path.exists():
            try:
                current = json.loads(config_path.read_text())
            except json.JSONDecodeError:
                current = {}
        else:
            current = {}

        current_mcp_servers = current.setdefault("mcpServers", {})
        try:
            previous_managed = set(json.loads(GEMINI_MANAGED_MCP_PATH.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            # Migration path for older jails: clean up the default yolo-managed
            # servers plus any stale workspace-bound servers from previous runs.
            previous_managed = {"chrome-devtools", "sequential-thinking"}
            for name, cfg in current_mcp_servers.items():
                if not isinstance(cfg, dict):
                    continue
                command = str(cfg.get("command", ""))
                if name.endswith("-lsp") and command == str(
                    GO_BIN / "mcp-language-server"
                ):
                    previous_managed.add(name)
                if command.startswith("/workspace/"):
                    previous_managed.add(name)

        for name in previous_managed:
            current_mcp_servers.pop(name, None)
        current_mcp_servers.update(configured_servers)

        current.setdefault("security", {})
        current["security"].setdefault("approvalMode", "yolo")
        current["security"].setdefault("enablePermanentToolApproval", True)
        current.setdefault("general", {})
        current["general"]["enableAutoUpdate"] = False
        current["general"]["enableAutoUpdateNotification"] = False

        config_path.write_text(json.dumps(current, indent=2) + "\n")
        GEMINI_MANAGED_MCP_PATH.write_text(
            json.dumps(sorted(configured_servers.keys()), indent=2) + "\n"
        )
    except Exception as e:
        print(f"Error configuring Gemini MCP: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 10. Claude Code config (MCP in settings.json, LSP plugins)
# ---------------------------------------------------------------------------

# Map jail LSP server names to Claude Code official plugin IDs.
CLAUDE_LSP_PLUGIN_MAP = {
    "python": "pyright-lsp@claude-plugins-official",
    "typescript": "typescript-lsp@claude-plugins-official",
    "go": "gopls-lsp@claude-plugins-official",
}


def _install_claude_plugins(plugin_map: dict, lsp_servers: dict):
    """Install Claude Code LSP plugins from the official marketplace.

    Reads installed_plugins.json to skip already-installed plugins.
    Uses `claude plugins install` for new ones.  Failures are non-fatal.
    """
    plugins_meta = CLAUDE_DIR / "plugins" / "installed_plugins.json"
    try:
        installed = set(json.loads(plugins_meta.read_text()).get("plugins", {}).keys())
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        installed = set()

    for lsp_name, plugin_id in plugin_map.items():
        if lsp_name not in lsp_servers:
            continue
        if plugin_id in installed:
            continue
        # Only attempt if claude binary is available
        claude_bin = HOME / ".local" / "bin" / "claude"
        if not claude_bin.exists():
            # Fall back to PATH (mise-installed or shim)
            claude_bin = Path("claude")
        try:
            subprocess.run(
                [str(claude_bin), "plugins", "install", plugin_id],
                capture_output=True,
                timeout=30,
                env={**os.environ, "YOLO_BYPASS_SHIMS": "1"},
            )
        except Exception:
            pass  # non-fatal — plugin will be installed on next boot


def _sync_host_claude_files():
    """Copy host ~/.claude/ files into the jail, except settings.json (merged separately)."""
    import json as _json

    host_claude_files = _json.loads(os.environ.get("YOLO_HOST_CLAUDE_FILES", "[]"))
    host_claude_dir = Path("/ctx/host-claude")

    for fname in host_claude_files:
        if fname == "settings.json":
            continue  # handled by configure_claude() via deep-merge
        src = host_claude_dir / fname
        dst = CLAUDE_DIR / fname
        if src.exists():
            try:
                shutil.copy2(str(src), str(dst))
            except shutil.SameFileError:
                pass  # nested jail — src and dst are the same inode
            except OSError as e:
                print(
                    f"Warning: could not copy host claude file {fname}: {e}",
                    file=sys.stderr,
                )


def _load_host_claude_settings() -> dict:
    """Load host settings.json from /ctx/host-claude/ if available."""
    import json as _json

    host_claude_files = _json.loads(os.environ.get("YOLO_HOST_CLAUDE_FILES", "[]"))
    if "settings.json" not in host_claude_files:
        return {}
    host_settings_path = Path("/ctx/host-claude/settings.json")
    if not host_settings_path.exists():
        return {}
    try:
        return _json.loads(host_settings_path.read_text())
    except (ValueError, OSError):
        return {}


def _isolate_claude_history():
    """Give each jail its own Claude Code prompt history (up-arrow isolation).

    Claude stores readline history in ~/.claude/history.jsonl — a single global
    file.  Since all jails share $HOME and all have cwd /workspace, the default
    history is shared across jails.

    Fix: replace history.jsonl with a symlink to a per-project file inside
    ~/.claude/jail-history/<hash>.jsonl, keyed on YOLO_HOST_DIR (the unique
    host workspace path).
    """
    host_dir = os.environ.get("YOLO_HOST_DIR", "")
    if not host_dir:
        return

    history_dir = CLAUDE_DIR / "jail-history"
    history_dir.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256(host_dir.encode()).hexdigest()[:12]
    per_jail = history_dir / f"{h}.jsonl"
    per_jail.touch(exist_ok=True)

    history_file = CLAUDE_DIR / "history.jsonl"
    # If it's already the right symlink, nothing to do
    if history_file.is_symlink():
        try:
            if history_file.resolve() == per_jail.resolve():
                return
        except OSError:
            pass
    # Remove existing (regular file or stale symlink)
    try:
        history_file.unlink(missing_ok=True)
    except OSError:
        pass
    history_file.symlink_to(per_jail)


def configure_claude():
    """Set up Claude Code: settings.json (permissions, plugins) + ~/.claude.json (MCP)."""
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    settings_path = CLAUDE_DIR / "settings.json"
    # Claude reads user-scoped MCP servers from ~/.claude.json, not settings.json.
    claude_json_path = HOME / ".claude.json"

    configured_servers = _load_mcp_servers()

    # Sync non-settings host claude files first
    _sync_host_claude_files()

    # Isolate prompt history per jail
    _isolate_claude_history()

    try:
        # --- settings.json: permissions, preferences, plugins ---
        # Start from host settings (deep-merge base), then layer YOLO overrides
        host_settings = _load_host_claude_settings()

        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except json.JSONDecodeError:
                settings = {}
        else:
            settings = {}

        # Deep-merge: host settings provide the base, existing jail settings
        # override, then YOLO-required keys override everything below.
        for key, val in host_settings.items():
            if key not in settings:
                settings[key] = val
            elif isinstance(val, dict) and isinstance(settings[key], dict):
                # Merge dicts one level deep (host fills gaps)
                for k, v in val.items():
                    if k not in settings[key]:
                        settings[key][k] = v

        # Remove any stale mcpServers from settings.json (moved to ~/.claude.json)
        settings.pop("mcpServers", None)

        # YOLO mode permissions — acceptEdits auto-approves tool use.
        # skipDangerousModePermissionPrompt suppresses the one-time confirmation
        # Claude shows when defaultMode is first set in a workspace.
        permissions = settings.setdefault("permissions", {})
        permissions["allow"] = [
            "Bash",
            "Edit",
            "Read",
            "WebFetch",
            "mcp__*",
            "Agent(*)",
        ]
        permissions["deny"] = []
        permissions["defaultMode"] = "acceptEdits"
        settings["skipDangerousModePermissionPrompt"] = True

        settings.setdefault("preferences", {})["autoUpdaterStatus"] = "disabled"

        # Enable LSP tool so Claude Code uses language servers for navigation.
        settings.setdefault("env", {})["ENABLE_LSP_TOOL"] = "1"

        # Enable LSP plugins matching the jail's configured LSP servers.
        lsp_servers = _load_lsp_servers()
        enabled_plugins = settings.setdefault("enabledPlugins", {})
        for lsp_name, plugin_id in CLAUDE_LSP_PLUGIN_MAP.items():
            if lsp_name in lsp_servers:
                enabled_plugins[plugin_id] = True

        settings_path.write_text(json.dumps(settings, indent=2) + "\n")

        # --- ~/.claude.json: user-scoped MCP servers ---
        if claude_json_path.exists():
            try:
                claude_json = json.loads(claude_json_path.read_text())
            except json.JSONDecodeError:
                claude_json = {}
        else:
            claude_json = {}

        mcp_servers = claude_json.setdefault("mcpServers", {})
        try:
            previous_managed = set(json.loads(CLAUDE_MANAGED_MCP_PATH.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            previous_managed = set()

        for name in previous_managed:
            mcp_servers.pop(name, None)
        mcp_servers.update(configured_servers)

        claude_json_path.write_text(json.dumps(claude_json, indent=2) + "\n")
        CLAUDE_MANAGED_MCP_PATH.write_text(
            json.dumps(sorted(configured_servers.keys()), indent=2) + "\n"
        )
    except Exception as e:
        print(f"Error configuring Claude: {e}", file=sys.stderr)

    # Install LSP plugins if not already present (idempotent, persists across restarts).
    _install_claude_plugins(CLAUDE_LSP_PLUGIN_MAP, _load_lsp_servers())


# ---------------------------------------------------------------------------
# 11. Cgroup delegation via host-side daemon (socket client)
# ---------------------------------------------------------------------------
# The host runs a cgroup delegate daemon that listens on a Unix socket at
# /tmp/yolo-cgd/cgroup.sock.  The container-side yolo-cglimit sends JSON
# requests to create child cgroups, set limits, and move processes.  This
# avoids needing CAP_SYS_ADMIN or rw cgroup mounts inside the container.
# All privileged cgroup operations happen on the host, with strict validation.

CGD_SOCKET = Path("/tmp/yolo-cgd/cgroup.sock")


def setup_cgroup_delegation():
    """Check if cgroup delegation is available via the host-side daemon.

    The host-side cgroup delegate daemon (started by cli.py) listens on a
    Unix socket mounted at /tmp/yolo-cgd/cgroup.sock.  This function just
    verifies the socket exists — all actual cgroup work is done by the host
    daemon when yolo-cglimit sends requests.

    Silent on absence: falls back to nice/timeout/ulimit in non-delegated jails.
    """
    if CGD_SOCKET.exists():
        print("  cgroup delegate: available (host-side daemon)", file=sys.stderr)
    else:
        print(
            "  cgroup delegate: not available (no host daemon socket)", file=sys.stderr
        )


def generate_cglimit_script():
    """Generate yolo-cglimit helper that delegates to the host-side cgroup daemon.

    Usage: yolo-cglimit [--cpu PCT] [--memory LIMIT] [--pids LIMIT] [--name NAME] -- COMMAND...
    Sends a request to the host-side daemon via Unix socket, which creates
    a child cgroup, sets limits, and moves the caller's process into it.
    """
    script_dir = HOME / ".local" / "bin"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "yolo-cglimit"

    # Python script that talks to the host daemon via Unix socket.
    # Uses only stdlib (socket, json, os, sys) — no pip deps.
    script_path.write_text(r'''#!/usr/bin/env python3
"""yolo-cglimit — Run a command under cgroup v2 resource limits.

Usage: yolo-cglimit [OPTIONS] -- COMMAND [ARGS...]

Options:
  --cpu PCT       CPU limit as percentage of ALL CPUs (e.g. 75 = 75% of total)
  --memory LIMIT  Memory limit (e.g. 512m, 2g, 1073741824)
  --pids LIMIT    Max number of processes
  --name NAME     Cgroup name (default: auto-generated from PID)

Examples:
  yolo-cglimit --cpu 75 -- python train.py           # 75% of all CPUs
  yolo-cglimit --cpu 50 --memory 2g -- make -j8      # 50% CPU + 2GB RAM
  yolo-cglimit --pids 100 -- ./fork-heavy-script.sh  # Max 100 processes

Resource limits are enforced by the kernel via cgroup v2 and cannot be exceeded.
The host-side daemon handles all privileged cgroup operations securely.
"""
import json
import os
import socket
import sys

CGD_SOCKET = "/tmp/yolo-cgd/cgroup.sock"


def send_request(request: dict) -> dict:
    """Send a JSON request to the host-side cgroup delegate daemon."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(CGD_SOCKET)
        sock.sendall((json.dumps(request) + "\n").encode())
        data = b""
        while b"\n" not in data and len(data) < 8192:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode())
    finally:
        sock.close()


def main():
    cpu_pct = None
    memory = None
    pids = None
    name = None
    command = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--cpu" and i + 1 < len(args):
            cpu_pct = int(args[i + 1])
            i += 2
        elif args[i] == "--memory" and i + 1 < len(args):
            memory = args[i + 1]
            i += 2
        elif args[i] == "--pids" and i + 1 < len(args):
            pids = int(args[i + 1])
            i += 2
        elif args[i] == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif args[i] == "--":
            command = args[i + 1:]
            break
        elif args[i] in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            print(f"Unknown option: {args[i]}", file=sys.stderr)
            sys.exit(1)

    if not command:
        print("Error: no command specified. Usage: yolo-cglimit [OPTIONS] -- COMMAND",
              file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(CGD_SOCKET):
        print("Error: cgroup delegation not available — host daemon socket not found.",
              file=sys.stderr)
        print("This requires the jail to be started with the yolo CLI (which runs the",
              file=sys.stderr)
        print("host-side cgroup delegate daemon automatically).", file=sys.stderr)
        sys.exit(1)

    # Build the request
    request = {"op": "create_and_join", "name": name or f"job-{os.getpid()}"}
    if cpu_pct is not None:
        request["cpu_pct"] = cpu_pct
    if memory is not None:
        request["memory"] = memory
    if pids is not None:
        request["pids"] = pids

    try:
        resp = send_request(request)
    except Exception as e:
        print(f"Error: failed to contact cgroup daemon: {e}", file=sys.stderr)
        sys.exit(1)

    if not resp.get("ok"):
        print(f"Error: {resp.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)

    if resp.get("warnings"):
        for w in resp["warnings"]:
            print(f"Warning: {w}", file=sys.stderr)

    # exec the command — we're already in the cgroup (daemon moved us via SO_PEERCRED)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
''')
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


# ---------------------------------------------------------------------------
# 11. Finalize PATH and exec bash
# ---------------------------------------------------------------------------


def exec_bash(command: str):
    """Set up final PATH, activate mise, and exec bash with the given command."""
    local_bin = HOME / ".local" / "bin"
    path = f"{SHIM_DIR}:{NPM_BIN}:{MISE_SHIMS}:{GO_BIN}:{local_bin}:/bin:/usr/bin"
    os.environ["PATH"] = path

    # Show what we're about to run for the exec-into-existing path.
    # For new containers, cli.py already embedded "Provisioning..." and "Executing..."
    # messages in the command string.  For plain interactive shells, skip the noise.
    is_new_container_cmd = "yolo-bootstrap" in command
    if command != "bash" and not is_new_container_cmd:
        sys.stderr.write(f"\033[1;36m⚡ Executing: {command}\033[0m\n")
        sys.stderr.flush()

    # Prepend mise env activation so tool paths (copilot, gemini, .venv/bin,
    # etc.) are available. Fresh containers get this from cli.py's inline
    # eval, but exec-into-existing skips that code path.
    activated_command = f'eval "$(mise env -s bash)" 2>/dev/null; {command}'

    os.execvp(
        "bash",
        [
            "bash",
            "--rcfile",
            str(BASHRC_PATH),
            "-c",
            activated_command,
        ],
    )


# ---------------------------------------------------------------------------
# Published port localhost fixup (iptables DNAT)
# ---------------------------------------------------------------------------


def setup_published_port_localnet():
    """Add iptables DNAT rules so published ports reach services bound to 127.0.0.1.

    Container runtimes forward published-port traffic to the container's network
    interface (eth0), not loopback.  Services that bind to 127.0.0.1 therefore
    never see it.  Combined with route_localnet=1 (set by cli.py via --sysctl),
    PREROUTING DNAT rules redirect arriving traffic to 127.0.0.1 — making
    published ports work regardless of the bind address inside the jail.

    Reads YOLO_PUBLISHED_PORTS (JSON array of "PORT/PROTO" strings).
    Silently skips if iptables is unavailable (e.g. Docker without NET_ADMIN).
    """
    raw = os.environ.get("YOLO_PUBLISHED_PORTS", "")
    if not raw:
        return

    try:
        ports = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print(f"Warning: invalid YOLO_PUBLISHED_PORTS: {raw}", file=sys.stderr)
        return

    if not ports:
        return

    iptables_bin = shutil.which("iptables")
    if not iptables_bin:
        return

    for entry in ports:
        parts = str(entry).split("/")
        port = parts[0]
        proto = parts[1] if len(parts) > 1 else "tcp"
        try:
            subprocess.run(
                [
                    iptables_bin,
                    "-t",
                    "nat",
                    "-A",
                    "PREROUTING",
                    "-p",
                    proto,
                    "--dport",
                    port,
                    "-j",
                    "DNAT",
                    "--to-destination",
                    f"127.0.0.1:{port}",
                ],
                capture_output=True,
                timeout=5,
            )
        except Exception as e:
            print(
                f"Warning: iptables DNAT for port {port}/{proto}: {e}",
                file=sys.stderr,
            )


def generate_yolo_wrapper():
    """Generate a yolo CLI wrapper in ~/.yolo-shims/.

    The host's mise-installed `yolo` console_script does `from src.cli import main`
    which fails inside the jail because the package isn't pip-installed there.
    mise activation can prepend installs/python/.../bin/ to PATH, so the wrapper
    must be in ~/.yolo-shims/ (first on PATH) to take priority.
    """
    repo_root = os.environ.get("YOLO_REPO_ROOT", "/opt/yolo-jail")
    SHIM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = SHIM_DIR / "yolo"
    # Use --no-project with explicit --with deps so uv doesn't need to find
    # or build the project (which fails on read-only /opt/yolo-jail mount and
    # when CWD is outside the project tree).
    script_path.write_text(f"""#!/bin/bash
exec uv run --no-project --with typer --with rich --with "pyjson5>=2.0.0" \
  -- python "{repo_root}/src/cli.py" "$@"
""")
    script_path.chmod(0o755)

    # Remove stale yolo wrapper from .local/bin if present — it was generated
    # by older entrypoint versions and lacks the --no-project fix.
    stale = HOME / ".local" / "bin" / "yolo"
    if stale.exists() and stale.is_file():
        stale.unlink()


# ---------------------------------------------------------------------------
# Host port forwarding (container side)
# ---------------------------------------------------------------------------

# The socket directory where host-side socat has already created Unix sockets.
FORWARD_SOCKET_DIR = Path("/tmp/yolo-fwd")


def _port_in_use(port: int) -> bool:
    """Check if a TCP port is already bound on localhost."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def start_container_port_forwarding():
    """Start container-side socat: TCP-LISTEN on localhost → UNIX-CONNECT to host socket.

    Reads YOLO_FORWARD_HOST_PORTS (JSON array). For each port, starts a socat
    that listens on container's 127.0.0.1:PORT and connects to the corresponding
    Unix socket at /tmp/yolo-fwd/port-PORT.sock (bind-mounted from host).

    The host side (cli.py) runs a matching socat that bridges the Unix socket to
    the host's 127.0.0.1:PORT. Together they form a tunnel analogous to SSH -L.

    Skips ports already bound (idempotent for container reuse via exec).
    """
    raw = os.environ.get("YOLO_FORWARD_HOST_PORTS", "")
    if not raw:
        return

    try:
        ports = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        print(f"Warning: invalid YOLO_FORWARD_HOST_PORTS: {raw}", file=sys.stderr)
        return

    if not ports:
        return

    log_path = HOME / ".yolo-socat.log"
    log_file = open(log_path, "a")

    for entry in ports:
        if isinstance(entry, int):
            local_port = entry
        elif isinstance(entry, str) and ":" in entry:
            local_port = int(entry.split(":", 1)[0])
        elif isinstance(entry, str):
            local_port = int(entry)
        else:
            print(f"Warning: invalid port forward entry: {entry}", file=sys.stderr)
            continue

        if _port_in_use(local_port):
            continue

        sock_path = FORWARD_SOCKET_DIR / f"port-{local_port}.sock"
        if not sock_path.exists():
            print(
                f"Warning: socket {sock_path} not found for port {local_port}",
                file=sys.stderr,
            )
            continue

        try:
            subprocess.Popen(
                [
                    "socat",
                    f"TCP-LISTEN:{local_port},bind=127.0.0.1,fork,reuseaddr",
                    f"UNIX-CONNECT:{sock_path}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=log_file,
            )
        except FileNotFoundError:
            print(
                "Warning: socat not found, cannot forward host ports", file=sys.stderr
            )
            log_file.close()
            return
        except Exception as e:
            print(f"Warning: failed to forward port {local_port}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    cmd = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "bash"
    _perf("start")

    # Each jail writes to its own per-workspace overlay dirs (mounted by cli.py),
    # so no flock needed — no cross-jail contention.
    generate_shims()
    _perf("generate_shims")
    generate_agent_launchers()
    _perf("generate_agent_launchers")
    generate_bashrc()
    _perf("generate_bashrc")
    generate_bootstrap_script()
    _perf("generate_bootstrap_script")
    generate_venv_precreate_script()
    _perf("generate_venv_precreate_script")
    generate_mise_config()
    _perf("generate_mise_config")

    # Copy host nvim config into the writable .config/ overlay.
    # In nested jails, src and dst may be the same inode (both point to the
    # shared .config overlay), so catch shutil.Error and skip silently.
    host_nvim = Path("/ctx/host-nvim-config")
    if host_nvim.is_dir():
        jail_nvim = HOME / ".config" / "nvim"
        jail_nvim.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(
                host_nvim,
                jail_nvim,
                symlinks=False,
                ignore_dangling_symlinks=True,
                dirs_exist_ok=True,
            )
        except shutil.Error:
            pass  # already in place (nested jail, same filesystem)
    _perf("nvim_config")

    generate_mcp_wrappers()
    _perf("generate_mcp_wrappers")
    configure_git()
    _perf("configure_git")
    configure_jj()
    _perf("configure_jj")
    # Skills are mounted :ro by cli.py — no entrypoint action needed.
    _perf("skills_skipped")
    configure_copilot()
    _perf("configure_copilot")
    configure_gemini()
    _perf("configure_gemini")
    configure_claude()
    _perf("configure_claude")
    setup_cgroup_delegation()
    _perf("cgroup_delegation")
    generate_cglimit_script()
    _perf("cglimit_script")
    generate_yolo_wrapper()
    _perf("yolo_wrapper")

    # These are per-container (use container-local network state), not shared
    setup_published_port_localnet()
    _perf("published_port_localnet")
    start_container_port_forwarding()
    _perf("port_forwarding")

    # Set PATH including mise shims so tools like copilot/gemini/claude are found
    os.environ["PATH"] = f"{SHIM_DIR}:{NPM_BIN}:{MISE_SHIMS}:{GO_BIN}:/bin:/usr/bin"

    # Trust workspace mise.toml (--quiet suppresses "No untrusted config files" noise)
    if Path("/workspace/mise.toml").exists():
        subprocess.run(
            ["mise", "trust", "--quiet", "/workspace/mise.toml"],
            capture_output=True,
        )

    # NOTE: We intentionally do NOT call `mise hook-env` here.
    # hook-env holds a WRITE flock, then spawns `uv` via the mise shim
    # (which IS /bin/mise), re-entering mise's flock → deadlock.
    # Instead, cli.py's setup_script calls ~/.yolo-venv-precreate.sh (after
    # `mise install`) to create venvs with real binaries, then uses
    # `eval "$(mise env -s bash)"` for stateless env activation.

    _perf_dump()

    exec_bash(cmd)


if __name__ == "__main__":
    main()
