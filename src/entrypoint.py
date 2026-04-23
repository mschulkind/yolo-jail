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
MISE_SHIMS = Path(os.environ["MISE_DATA_DIR"]) / "shims"
MCP_WRAPPERS_BIN = HOME / ".local" / "bin" / "mcp-wrappers"
BASHRC_PATH = HOME / ".bashrc"
COPILOT_DIR = HOME / ".copilot"
GEMINI_DIR = HOME / ".gemini"
GEMINI_MANAGED_MCP_PATH = GEMINI_DIR / "yolo-managed-mcp-servers.json"
CLAUDE_DIR = HOME / ".claude"
CLAUDE_MANAGED_MCP_PATH = CLAUDE_DIR / "yolo-managed-mcp-servers.json"
CLAUDE_SHARED_CREDENTIALS_DIR = HOME / ".claude-shared-credentials"
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
        # ``block_flags`` is a list of shell ``case`` glob patterns.
        # When present, the shim only blocks when argv contains one
        # of the patterns; otherwise argv passes through to the real
        # binary.  Absent means "always block" (default for ``find``).
        block_flags = tool_cfg.get("block_flags") or []

        if block_flags and real_bin:
            # Split patterns into explicit long-option exact matches
            # (``--foo``) and everything else.  The shim emits the
            # long matches first, then a wildcard ``--*`` skip so
            # unrelated long options (``--regex`` when the user
            # configured short pattern ``-*[rR]*``) don't get caught,
            # then the short patterns.
            long_exact = [p for p in block_flags if p.startswith("--")]
            short_patterns = [p for p in block_flags if not p.startswith("--")]

            lines = ["#!/bin/sh"]
            lines.append('if [ -z "$YOLO_BYPASS_SHIMS" ]; then')
            lines.append('  for arg in "$@"; do')
            lines.append('    case "$arg" in')
            if long_exact:
                lines.append("      " + "|".join(long_exact) + ")")
                lines.append(f'        echo "{msg}" >&2')
                if sug:
                    lines.append(f'        echo "Suggestion: {sug}" >&2')
                lines.append("        exit 127")
                lines.append("        ;;")
            lines.append("      --*)")
            lines.append("        : ;;")
            if short_patterns:
                lines.append("      " + "|".join(short_patterns) + ")")
                lines.append(f'        echo "{msg}" >&2')
                if sug:
                    lines.append(f'        echo "Suggestion: {sug}" >&2')
                lines.append("        exit 127")
                lines.append("        ;;")
            lines.append("    esac")
            lines.append("  done")
            lines.append("fi")
            lines.append(f'exec {real_bin} "$@"')
            lines.append("")
        else:
            # Unconditional block.
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
    mise_shims = str(MISE_SHIMS)

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

# Combined CA bundle — baseline Nix cacert + every loophole CA.
# Point every standard TLS trust-store env var at one file so Python
# (ssl, requests, httpx), curl, and git all verify the same roots the
# in-jail broker leafs are signed by.  NODE_EXTRA_CA_CERTS is set by
# the container launcher to just the extras (Node adds them to its own
# bundled roots).  See generate_ca_bundle() in entrypoint.py.
if [ -f "$HOME/.yolo-ca-bundle.crt" ]; then
    export SSL_CERT_FILE="$HOME/.yolo-ca-bundle.crt"
    export REQUESTS_CA_BUNDLE="$HOME/.yolo-ca-bundle.crt"
    export CURL_CA_BUNDLE="$HOME/.yolo-ca-bundle.crt"
    export GIT_SSL_CAINFO="$HOME/.yolo-ca-bundle.crt"
fi

# Source user-defined env vars from config (defaults, overridable by .env).
# Loaded early so mise activation can override with .env values.
[ -f "$HOME/.config/yolo-user-env.sh" ] && . "$HOME/.config/yolo-user-env.sh"

# PATH with npm-global and go binaries
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
export NPM_CONFIG_CACHE="${NPM_CONFIG_CACHE:-$HOME/.cache/npm}"
export GOPATH="${GOPATH:-$HOME/go}"
SHIM_DIR="${HOME}/.yolo-shims"
export PATH="$SHIM_DIR:$HOME/.local/bin:$NPM_CONFIG_PREFIX/bin:"""
        + mise_shims
        + r""":$GOPATH/bin:/bin:/usr/bin"

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
# 2a. Combined CA bundle — so every TLS client finds the loophole CAs
# ---------------------------------------------------------------------------

# The combined bundle.  Writable path under $HOME so we can rewrite it at
# every jail boot.


def _read_bundle_bytes(path: Path) -> bytes:
    """Read a PEM file, returning b'' on any error.  Not finding a cert
    source is a warn, not a fatal — the combined bundle is best-effort
    and we always keep going.  The baseline bundle is usually present
    via the image env var; individual loophole CAs can be absent if the
    loophole hasn't been primed yet."""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def generate_ca_bundle() -> Path:
    """Build ``$HOME/.yolo-ca-bundle.crt`` from the image baseline +
    every loophole CA, and point the standard env vars at it.

    Order of contents:
      1. the image's baseline bundle (``$SSL_CERT_FILE``; set at image
         build time to the Nix cacert Mozilla bundle), if readable.
      2. each path in ``$NODE_EXTRA_CA_CERTS`` — that's the colon-
         separated list cli.py assembles from every active loophole's
         ``ca_cert`` field.

    The resulting bundle is exported via ``os.environ`` so any child
    process spawned from the entrypoint (jail-daemon supervisor, bash,
    etc.) sees the combined trust store under the usual var names.
    The bashrc re-exports the same vars for interactive shells.
    """
    # Refresh CA_BUNDLE_PATH in case HOME was monkeypatched (tests).
    bundle_path = HOME / ".yolo-ca-bundle.crt"

    chunks: list[bytes] = []
    baseline = os.environ.get("SSL_CERT_FILE", "")
    if baseline and baseline != str(bundle_path):
        data = _read_bundle_bytes(Path(baseline))
        if data:
            chunks.append(data)

    extras = os.environ.get("NODE_EXTRA_CA_CERTS", "")
    if extras:
        seen: set[str] = set()
        for raw in extras.split(os.pathsep):
            p = raw.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            data = _read_bundle_bytes(Path(p))
            if data:
                chunks.append(data)

    # Always write a file, even if empty — env vars pointing at a
    # nonexistent path confuse some tools (curl prints a warning on
    # every request).  An empty bundle is harmless: baseline-only
    # verification still works via the image default if set.
    body = b"\n".join(c.rstrip(b"\n") for c in chunks)
    if body and not body.endswith(b"\n"):
        body += b"\n"
    bundle_path.write_bytes(body)
    os.chmod(bundle_path, 0o644)

    # Point the standard vars at the combined bundle so children inherit
    # the right trust store without having to know about this file.
    bundle_str = str(bundle_path)
    os.environ["SSL_CERT_FILE"] = bundle_str
    os.environ["REQUESTS_CA_BUNDLE"] = bundle_str
    os.environ["CURL_CA_BUNDLE"] = bundle_str
    os.environ["GIT_SSL_CAINFO"] = bundle_str
    return bundle_path


# ---------------------------------------------------------------------------
# 3. Bootstrap script (runs after mise is ready)
# ---------------------------------------------------------------------------


def generate_bootstrap_script():
    """Create the idempotent bootstrap script that installs MCP/LSP tools."""
    script_path = HOME / ".yolo-bootstrap.sh"
    mise_shims = str(MISE_SHIMS)
    script_path.write_text(rf"""#!/bin/bash
export NPM_CONFIG_PREFIX="${{NPM_CONFIG_PREFIX:-$HOME/.npm-global}}"
export NPM_CONFIG_CACHE="${{NPM_CONFIG_CACHE:-$HOME/.cache/npm}}"
export GOPATH="${{GOPATH:-$HOME/go}}"
export GOBIN="$GOPATH/bin"
export PATH="$HOME/.local/bin:$NPM_CONFIG_PREFIX/bin:{mise_shims}:$GOBIN:$PATH"

# Initialize font cache (once, not on every shell session)
fc-cache -f >/dev/null 2>&1

# Agent CLIs (gemini, copilot, claude) are NOT updated here.
# Lazy-update launchers in ~/.yolo-shims/ handle install/update on first use,
# keeping boot fast.  Only MCP/LSP tools that agents depend on are installed here.

# Install binaries if missing.
if ! command -v chrome-devtools-mcp >/dev/null; then
    echo "  Installing MCP tools..." >&2
    # Clean stale npm temp directories that cause ENOTEMPTY on rename.
    # maxdepth 2 catches both top-level and scoped (@org/) packages.
    find "$NPM_CONFIG_PREFIX/lib/node_modules" -maxdepth 2 -name '.*' -type d 2>/dev/null | xargs rm -rf
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
        merged = {**base_tools, **injected_tools}
        lines = ["[tools]"]
        for tool, version in merged.items():
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

    # Self-heal: drop duplicate tool-key lines (keep the first). A prior bug
    # could write a base tool twice when a workspace also injected it, and mise
    # refuses to parse the resulting file.
    seen_keys: set[str] = set()
    deduped_lines: list[str] = []
    key_re = re.compile(r'^\s*"?([^"\s=]+)"?\s*=')
    for line in content.splitlines(keepends=True):
        m = key_re.match(line)
        if m:
            key = m.group(1)
            if key in seen_keys:
                changed = True
                continue
            seen_keys.add(key)
        deduped_lines.append(line)
    if changed:
        content = "".join(deduped_lines)

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


def _credentials_expiry(path: Path) -> int:
    """Return the expiresAt timestamp (ms) from a Claude credentials file, or 0."""
    import json as _json

    try:
        data = _json.loads(path.read_text())
        oauth = data.get("claudeAiOauth") or {}
        return int(oauth.get("expiresAt", 0))
    except (ValueError, OSError, KeyError):
        return 0


def _sync_host_claude_files():
    """Copy host ~/.claude/ files into the jail, except settings.json (merged separately).

    For .credentials.json, keeps the freshest token — if the jail already has
    credentials with a later expiry (from a prior /login), the host copy is
    skipped so the user doesn't have to re-login in every jail.
    """
    import json as _json

    host_claude_files = _json.loads(os.environ.get("YOLO_HOST_CLAUDE_FILES", "[]"))
    host_claude_dir = Path("/ctx/host-claude")

    for fname in host_claude_files:
        if fname == "settings.json":
            continue  # handled by configure_claude() via deep-merge
        src = host_claude_dir / fname
        # Credentials live in the shared dir (directory bind mount that
        # supports atomic rename).  Other files go to the per-workspace
        # .claude/ overlay as before.
        if fname == ".credentials.json":
            dst = CLAUDE_SHARED_CREDENTIALS_DIR / fname
        else:
            dst = CLAUDE_DIR / fname
        if not src.exists():
            continue
        # For credentials: keep the token with the later expiry.
        if fname == ".credentials.json":
            dst_expiry = _credentials_expiry(dst)
            src_expiry = _credentials_expiry(src)
            if dst_expiry >= src_expiry and dst_expiry > 0:
                continue  # jail credentials are fresher — keep them
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


def _ensure_credentials_symlink():
    """Ensure .claude/.credentials.json is a symlink into the shared credentials dir.

    The shared credentials directory is a rw directory bind mount, so Claude
    Code's IWH atomic writer (readlinkSync → tmp → rename) works correctly.
    The old approach — a single-file bind mount — caused EBUSY on rename,
    forcing the fallback truncate+write path which can lose data in races.
    """
    link = CLAUDE_DIR / ".credentials.json"
    target = Path("..") / ".claude-shared-credentials" / ".credentials.json"

    if link.is_symlink():
        try:
            if Path(os.readlink(str(link))) == target:
                return  # already correct
        except OSError:
            pass
        link.unlink()
    elif link.exists():
        # Migration: existing regular file (from old single-file bind mount era).
        # Copy its data to the shared dir if the shared dir's copy is missing
        # or empty, then replace with symlink.
        shared = CLAUDE_SHARED_CREDENTIALS_DIR / ".credentials.json"
        if not shared.exists() or shared.stat().st_size == 0:
            try:
                shutil.copy2(str(link), str(shared))
            except OSError:
                pass
        try:
            link.unlink()
        except OSError:
            return  # can't remove — leave as-is (still works via fallback write)

    link.symlink_to(target)


def configure_claude():
    """Set up Claude Code: settings.json (permissions, plugins) + ~/.claude.json (MCP)."""
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    settings_path = CLAUDE_DIR / "settings.json"
    # Claude reads user-scoped MCP servers from ~/.claude.json, not settings.json.
    claude_json_path = HOME / ".claude.json"

    configured_servers = _load_mcp_servers()

    # Ensure .credentials.json is a symlink into the shared credentials dir.
    # Claude Code's IWH atomic writer resolves symlinks before writing, so
    # tmp+rename happens in the directory mount (where rename works) instead
    # of on the old single-file bind mount (where rename returned EBUSY).
    _ensure_credentials_symlink()

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
        #
        # MCP rules: Claude's permission matcher (`w46` in the 2.1.x binary)
        # uses strict equality on server name — there is NO glob matching.
        # `"mcp__*"` parses via `Om()` to {serverName: "*", toolName: undefined}
        # which then fails the `K.serverName === _.serverName` check against
        # any real server.  We have to enumerate the configured servers and
        # emit one `mcp__<name>` rule per server.  The rule with no
        # double-underscore suffix matches ALL tools of that server because
        # `Om("mcp__foo")` returns toolName=undefined, and the matcher accepts
        # `K.toolName === void 0 || K.toolName === "*"` as "any tool".
        mcp_allow_rules = [f"mcp__{name}" for name in sorted(configured_servers)]

        permissions = settings.setdefault("permissions", {})
        # Wildcard pattern (Tool(*)) is required — bare tool names
        # like "Bash" match "the Bash tool with no pattern", which
        # doesn't match any real invocation, so every Bash(...) call
        # falls through to the prompt.  Claude Code's matcher only
        # pattern-compares rules with parentheses, so we have to
        # provide the universal pattern explicitly.
        permissions["allow"] = [
            "Bash(*)",
            "Edit(*)",
            "Read(*)",
            "WebFetch(*)",
            *mcp_allow_rules,
            "Agent(*)",
        ]
        permissions["deny"] = []
        permissions["defaultMode"] = "acceptEdits"
        # Pre-authorize reads everywhere. The jail container is the security
        # boundary; whatever is reachable from inside is already scoped. A
        # per-directory allowlist was whack-a-mole (forgot /ctx, etc.); "/"
        # matches every path so we stop playing.
        permissions["additionalDirectories"] = ["/"]
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

        # Belt-and-suspenders: mark the /workspace project as auto-approving
        # all its MCP servers.  This suppresses any secondary trust dialog
        # Claude may fire on first use of a server (`Dx$` in the 2.1.x binary
        # checks `enableAllProjectMcpServers` before returning "pending").
        # The permission-rule fix above handles the per-tool prompts; this
        # handles the per-server trust dialog, if one applies.
        workspace_project = claude_json.setdefault("projects", {}).setdefault(
            "/workspace", {}
        )
        workspace_project["enableAllProjectMcpServers"] = True
        workspace_project.setdefault("hasTrustDialogAccepted", True)

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
# /run/yolo-services/cgroup-delegate.sock.  The container-side yolo-cglimit
# sends JSON requests to create child cgroups, set limits, and move
# processes.  This avoids needing CAP_SYS_ADMIN or rw cgroup mounts inside
# the container.  All privileged cgroup operations happen on the host, with
# strict validation.
#
# The cgroup delegate is one of several host-side services that yolo-jail
# can run alongside the container.  See cli.py § "Host services" for the
# generic mechanism — user-defined services in `host_services` config also
# appear under /run/yolo-services/.

CGD_SOCKET = Path("/run/yolo-services/cgroup-delegate.sock")


def setup_cgroup_delegation():
    """Check if cgroup delegation is available via the host-side daemon.

    The host-side cgroup delegate daemon (started by cli.py) listens on a
    Unix socket mounted at /run/yolo-services/cgroup-delegate.sock.  This
    function just verifies the socket exists — all actual cgroup work is
    done by the host daemon when yolo-cglimit sends requests.

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

CGD_SOCKET = "/run/yolo-services/cgroup-delegate.sock"


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


def generate_yolo_ps_script():
    """Drop a ``yolo-ps`` wrapper into ``~/.local/bin/`` inside the jail.

    The host-processes loophole ships its jail-side CLI as the
    ``yolo-ps`` wheel console script.  Wheels aren't installed inside
    the jail, so we generate a tiny wrapper that invokes
    ``src.yolo_ps:main`` from the bind-mounted repo root instead.

    Same pattern as ``generate_journalctl_script`` / ``generate_yolo_wrapper``:
    no dependency on PYTHONPATH and no cd dance — just a
    ``sys.path.insert`` before the import.
    """
    repo_root = os.environ.get("YOLO_REPO_ROOT", "/opt/yolo-jail")
    script_dir = HOME / ".local" / "bin"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "yolo-ps"
    script_path.write_text(
        f"""#!/usr/bin/env python3
\"\"\"yolo-ps — jail-side client for the host-processes loophole.
Thin wrapper that invokes src.yolo_ps:main from the bind-mounted
yolo-jail repo root.
\"\"\"
import sys
sys.path.insert(0, {repo_root!r})
from src.yolo_ps import main
sys.exit(main())
"""
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


def generate_journalctl_script():
    """Generate yolo-journalctl helper that bridges to a host-side daemon.

    Usage: yolo-journalctl [journalctl args...]

    The helper reads YOLO_SERVICE_JOURNAL_SOCKET and connects to the host
    daemon over Unix socket, sends its argv as a single JSON line, and
    decodes framed [stdout/stderr/exit] responses until the daemon closes
    the connection.  Exits with the exit code the daemon reports (the code
    journalctl returned on the host).

    The daemon only runs if the user enabled it in config via
    `journal: "user"` or `"full"`.  When the socket is absent the helper
    prints a clear hint and exits 1 — that's the signal the user hasn't
    opted in.
    """
    script_dir = HOME / ".local" / "bin"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "yolo-journalctl"

    script_path.write_text(r'''#!/usr/bin/env python3
"""yolo-journalctl — Run journalctl on the host via the yolo-jail journal bridge.

Usage: yolo-journalctl [journalctl args...]

Forwards all arguments to `journalctl` running on the host, streams stdout
and stderr back to the local terminal, and exits with the host process's
exit code.  Enabled only when the jail's config sets `journal: "user"`
(forces --user) or `journal: "full"` (unrestricted).

Examples:
  yolo-journalctl -u nginx -n 50
  yolo-journalctl --user -f
  yolo-journalctl -p err --since "1 hour ago"
"""
import json
import os
import socket
import struct
import sys

DEFAULT_SOCKET = "/run/yolo-services/journal.sock"
SOCKET_PATH = os.environ.get("YOLO_SERVICE_JOURNAL_SOCKET", DEFAULT_SOCKET)

FRAME_STDOUT = 1
FRAME_STDERR = 2
FRAME_EXIT = 3


def _read_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


def main():
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help") and not os.environ.get("YOLO_JOURNALCTL_PASSTHROUGH_HELP"):
        # -h/--help without env override prints our own doc, not journalctl's.
        # Set YOLO_JOURNALCTL_PASSTHROUGH_HELP=1 to forward it through.
        print(__doc__)
        print(f"Socket: {SOCKET_PATH}")
        return 0

    if not os.path.exists(SOCKET_PATH):
        sys.stderr.write(
            "yolo-journalctl: host journal bridge is not available.\n"
        )
        sys.stderr.write(
            f"  expected socket: {SOCKET_PATH}\n"
        )
        sys.stderr.write(
            "  enable it by setting `journal: \"user\"` (or \"full\") in yolo-jail.jsonc\n"
        )
        sys.stderr.write(
            "  or in ~/.config/yolo-jail/config.jsonc, then restart the jail.\n"
        )
        return 1

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET_PATH)
    except OSError as e:
        sys.stderr.write(f"yolo-journalctl: connect failed: {e}\n")
        return 1

    try:
        sock.sendall((json.dumps({"args": args}) + "\n").encode())
        exit_code = 1
        while True:
            header = _read_exact(sock, 5)
            if len(header) < 5:
                break
            stream, length = struct.unpack(">BI", header)
            payload = _read_exact(sock, length)
            if len(payload) < length:
                break
            if stream == FRAME_STDOUT:
                sys.stdout.buffer.write(payload)
                sys.stdout.flush()
            elif stream == FRAME_STDERR:
                sys.stderr.buffer.write(payload)
                sys.stderr.flush()
            elif stream == FRAME_EXIT:
                if len(payload) == 4:
                    (exit_code,) = struct.unpack(">i", payload)
                break
            else:
                # Unknown frame type — ignore, forward-compat.
                continue
        return exit_code
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
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

    # Source user-defined env vars from config (defaults, overridable by .env).
    # Then activate mise so tool paths (copilot, gemini, .venv/bin, etc.) are
    # available.  Mise env runs AFTER user-env so .env can override config vars.
    # Fresh containers get mise activation from cli.py's inline eval, but
    # exec-into-existing skips that code path.
    user_env_file = HOME / ".config" / "yolo-user-env.sh"
    source_user_env = (
        f'. "{user_env_file}" 2>/dev/null; ' if user_env_file.exists() else ""
    )
    activated_command = (
        f'{source_user_env}eval "$(mise env -s bash)" 2>/dev/null; {command}'
    )

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


# /tmp is a per-container tmpfs on both podman and docker, so a PID
# file here is naturally scoped to this jail and evaporates on restart.
# Serves as the single-instance lock for the supervisor: entrypoint.main()
# re-runs on every ``podman exec yolo-entrypoint <cmd>``, and without
# this guard each exec would fork another supervisor that tries to
# bind the same port (:443 for the oauth broker) and crashloops on
# EADDRINUSE.  See handover #3 for the full story.
SUPERVISOR_PID_FILE = Path("/tmp/yolo-jail-supervisor.pid")


def _supervisor_is_alive(pid_file: Path) -> bool:
    """Read ``pid_file`` and return True iff the PID it names is still a
    live process.  Missing/unreadable/corrupt file → False.  Signal 0
    is the canonical Unix liveness probe — it permission-checks the
    target but doesn't actually deliver anything."""
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but we can't signal it — still counts as alive
        # for our purposes (don't spawn another).
        return True
    except OSError:
        return False
    return True


def start_jail_daemon_supervisor():
    """Fork ``src.jail_daemon_supervisor`` as a detached child, once.

    The supervisor reads ``YOLO_JAIL_DAEMONS`` from the env and spawns
    each loophole-declared jail daemon with restart-on-failure
    semantics.  Absent or empty env means nothing to do.

    Guarded by a tmpfs PID file so repeated ``podman exec yolo-entrypoint``
    calls (the way every ``yolo -- <cmd>`` after the first lands) don't
    stack additional supervisors inside the same container.  Extras
    would each try to bind the same loophole port and crashloop.

    We launch via ``python -m`` rather than a direct import + fork to
    keep the supervisor out of the entrypoint's GC roots and let it
    evolve independently.  The child inherits PID 1's env, including
    the daemon list.
    """
    if not os.environ.get("YOLO_JAIL_DAEMONS", "").strip():
        return
    if _supervisor_is_alive(SUPERVISOR_PID_FILE):
        return
    repo_root = os.environ.get("YOLO_REPO_ROOT", "/opt/yolo-jail")
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.jail_daemon_supervisor"],
        env={**os.environ, "PYTHONPATH": repo_root},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=False,  # stay in the same process group as PID 1
    )
    try:
        SUPERVISOR_PID_FILE.write_text(f"{proc.pid}\n")
    except OSError:
        # Best-effort: losing the PID file just means a re-entrant
        # exec may spawn a redundant supervisor.  Don't abort boot.
        pass


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
    # Two broken approaches this design avoids:
    #
    # 1. ``export PYTHONPATH={repo_root}`` — ``uv run`` doesn't
    #    reliably honor PYTHONPATH (it manages its own environment
    #    for the ephemeral venv), so ``from src.cli import main``
    #    fails with ModuleNotFoundError intermittently.
    # 2. ``cd {repo_root}`` — the repo root is a read-only bind
    #    mount, and uv's getcwd() fails on bind-mounted CWDs with
    #    "Current directory does not exist" before the Python child
    #    even starts.
    #
    # Instead: a tiny bootstrap Python file in the writable shim dir
    # does the sys.path insert before importing.  The shim runs
    # ``uv run -- python {bootstrap}`` from whatever CWD the user is
    # in (normal writable directory), so neither of the above bites.
    bootstrap_py = SHIM_DIR / "_yolo_bootstrap.py"
    bootstrap_py.write_text(f'''#!/usr/bin/env python3
"""Make ``src`` importable without PYTHONPATH or cd gymnastics."""
import sys
sys.path.insert(0, {repo_root!r})
# Rewrite argv[0] so typer's help/usage strings read "yolo", not
# this bootstrap path.
sys.argv[0] = "yolo"
from src.cli import main

main()
''')
    bootstrap_py.chmod(0o755)
    script_path.write_text(f"""#!/bin/bash
exec uv run --no-project --with typer --with rich --with "pyjson5>=2.0.0" \
  -- python "{bootstrap_py}" "$@"
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
    """Start container-side socat: TCP-LISTEN on localhost → host service.

    Reads YOLO_FORWARD_HOST_PORTS (JSON array). For each port, starts a socat
    that listens on container's 127.0.0.1:PORT.

    Two modes depending on environment:
    - Unix socket mode (Linux): connects to /tmp/yolo-fwd/port-PORT.sock
      (bind-mounted from host where host-side socat bridges to host localhost).
    - TCP gateway mode (macOS): connects to YOLO_FWD_HOST_GATEWAY:PORT
      directly via TCP (host.docker.internal resolves to the host).

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

    # Determine forwarding mode
    host_gateway = os.environ.get("YOLO_FWD_HOST_GATEWAY", "")

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

        if host_gateway:
            # TCP gateway mode: connect directly to host via Docker gateway
            target = f"TCP:{host_gateway}:{local_port}"
        else:
            # Unix socket mode: connect to bind-mounted socket from host
            sock_path = FORWARD_SOCKET_DIR / f"port-{local_port}.sock"
            if not sock_path.exists():
                print(
                    f"Warning: socket {sock_path} not found for port {local_port}",
                    file=sys.stderr,
                )
                continue
            target = f"UNIX-CONNECT:{sock_path}"

        try:
            subprocess.Popen(
                [
                    "socat",
                    f"TCP-LISTEN:{local_port},bind=127.0.0.1,fork,reuseaddr",
                    target,
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
    # Build the combined CA bundle BEFORE bashrc so bashrc can just
    # reference ``$HOME/.yolo-ca-bundle.crt`` and the env vars we set
    # in ``os.environ`` propagate to every child the entrypoint spawns
    # (jail daemon supervisor, port-forwarders, etc.) ahead of bash.
    generate_ca_bundle()
    _perf("generate_ca_bundle")
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
    generate_journalctl_script()
    _perf("journalctl_script")
    generate_yolo_ps_script()
    _perf("yolo_ps_script")
    generate_yolo_wrapper()
    _perf("yolo_wrapper")

    # These are per-container (use container-local network state), not shared
    setup_published_port_localnet()
    _perf("published_port_localnet")
    start_container_port_forwarding()
    _perf("port_forwarding")

    # Start the jail-daemon supervisor if any loopholes declared a
    # ``jail_daemon``.  Runs as a child of PID 1; kernel kills it when
    # PID 1 exits so no explicit teardown is needed.
    start_jail_daemon_supervisor()
    _perf("jail_daemon_supervisor")

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
