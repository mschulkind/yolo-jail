#!/bin/bash
# YOLO Jail Entrypoint Script

# 1. Create a writable directory for dynamic shims in the persistent store
SHIM_DIR="$HOME/.yolo-shims"
rm -rf "$SHIM_DIR"
mkdir -p "$SHIM_DIR"

# 2. Read blocked tools from environment variable (injected by Python CLI)
if [ -n "$YOLO_BLOCK_CONFIG" ]; then
    SHIM_DIR="$SHIM_DIR" python3 <<'PYSHIMS'
import json, os, sys, stat

shim_dir = os.environ.get('SHIM_DIR')
try:
    config = json.loads(os.environ['YOLO_BLOCK_CONFIG'])
    for tool_cfg in config:
        name = tool_cfg.get('name')
        if not name: continue
        
        msg = tool_cfg.get('message', f'Error: tool {name} is blocked in this project.')
        sug = tool_cfg.get('suggestion', '')
        
        shim_path = os.path.join(shim_dir, name)
        
        # Determine the real binary path for tools that have one
        real_bin = f'/bin/{name}' if name in ['grep', 'find'] else None
        
        if real_bin:
            content = f'''#!/bin/sh
if [ -z "$YOLO_BYPASS_SHIMS" ]; then
  echo "{msg}" >&2
  [ -n "{sug}" ] && echo "Suggestion: {sug}" >&2
  exit 127
fi
exec {real_bin} "$@"
'''
        else:
            content = f'''#!/bin/sh
if [ -z "$YOLO_BYPASS_SHIMS" ]; then
  echo "{msg}" >&2
  [ -n "{sug}" ] && echo "Suggestion: {sug}" >&2
  exit 127
fi
'''
        
        with open(shim_path, 'w') as f:
            f.write(content)
        
        st = os.stat(shim_path)
        os.chmod(shim_path, st.st_mode | stat.S_IEXEC)
        
except Exception as e:
    sys.stderr.write(f'Error generating shims: {e}\n')
PYSHIMS
fi

# 5. Set up a colorful prompt in the persistent store
BASHRC="$HOME/.bashrc"
cat <<'EOF' > "$BASHRC"
# YOLO Jail Prompt
YELLOW='\[\033[1;33m\]'
RED='\[\033[1;31m\]'
GREEN='\[\033[1;32m\]'
BLUE='\[\033[1;34m\]'
MAGENTA='\[\033[1;35m\]'
CYAN='\[\033[1;36m\]'
NC='\[\033[0m\]' # No Color

# Big colorful warning
JAIL_BANNER="${RED}🔒 YOLO-JAIL${NC}"
HOST_INFO="${CYAN}(host: ${YOLO_HOST_DIR:-unknown})${NC}"

export PS1="\n${JAIL_BANNER} ${HOST_INFO}\n${GREEN}jail${NC}:${BLUE}\w${NC}\$ "

# Set PROMPT_COMMAND to update tmux window title on every prompt
# This overrides tmux's automatic-rename which shows process names
export PROMPT_COMMAND='printf "\033]0;JAIL\033\\"'

# Initialize font cache for Chromium
fc-cache -f >/dev/null 2>&1

# Agent-friendly defaults (no pagers, no line numbers)
export PAGER=cat
export BAT_PAGER=""
export BAT_STYLE="plain"
export GIT_PAGER=cat
export EDITOR=nvim

# Setup PATH with npm-global and go binaries (from docker env variables)
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
export GOPATH="${GOPATH:-$HOME/go}"
SHIM_DIR="${HOME}/.yolo-shims"
export PATH="$SHIM_DIR:$NPM_CONFIG_PREFIX/bin:$GOPATH/bin:/bin:/usr/bin"

# Activate mise with shell hooks so it can manage venvs, env vars, etc.
eval "$(mise activate bash)"

# Aliases
alias ls='ls --color=auto'
alias ll='ls -alF'
alias gemini='gemini --yolo'
alias copilot='copilot --yolo'
alias vi='nvim'
alias vim='nvim'
alias bat='bat --style=plain --paging=never'
EOF

# 6. Bootstrap Default Agent Configs (YOLO Mode)
AGENT_HOME="${JAIL_HOME:-/home/agent}"

# Let npm use default global prefix in home
export NPM_CONFIG_PREFIX="$AGENT_HOME/.npm-global"

# Create a bootstrap script that will run AFTER mise is ready
BOOTSTRAP_SCRIPT="$AGENT_HOME/.yolo-bootstrap.sh"
cat <<'EOF' > "$BOOTSTRAP_SCRIPT"
#!/bin/bash
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
export GOPATH="${GOPATH:-$HOME/go}"
export GOBIN="$GOPATH/bin"
export PATH="$NPM_CONFIG_PREFIX/bin:$GOBIN:$PATH"

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
EOF
chmod +x "$BOOTSTRAP_SCRIPT"

# Global Mise Config
MISE_CONFIG_DIR="$AGENT_HOME/.config/mise"
if [ ! -f "$MISE_CONFIG_DIR/config.toml" ]; then
    mkdir -p "$MISE_CONFIG_DIR"
    cat <<EOF > "$MISE_CONFIG_DIR/config.toml"
[tools]
node = "22"
python = "3.13"
go = "latest"
"npm:@google/gemini-cli" = "latest"
"npm:@github/copilot" = "latest"
EOF
fi

# Chrome Wrapper Script for MCP (avoids pipe-mode fd conflicts when spawned by agents)
# This runs at startup before bootstrap, so we need to embed the npm prefix discovery
CHROME_WRAPPER="$AGENT_HOME/.local/bin/chrome-devtools-mcp-wrapper"
mkdir -p "$(dirname "$CHROME_WRAPPER")"
cat >"$CHROME_WRAPPER" <<'WRAPPER'
#!/bin/bash
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
WRAPPER
chmod +x "$CHROME_WRAPPER"

# Node/npx wrappers that set LD_LIBRARY_PATH for mise-installed binaries
# Agents (Copilot) may sanitize the environment when spawning MCP servers,
# stripping LD_LIBRARY_PATH. These wrappers ensure shared libs are found.
# Placed in .local/bin/mcp-wrappers/ to avoid conflicting with mise shims.
MCP_BIN="$AGENT_HOME/.local/bin/mcp-wrappers"
mkdir -p "$MCP_BIN"

NODE_WRAPPER="$MCP_BIN/node"
cat <<'NODEW' > "$NODE_WRAPPER"
#!/bin/bash
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
exec /mise/shims/node "$@"
NODEW
chmod +x "$NODE_WRAPPER"

NPX_WRAPPER="$MCP_BIN/npx"
cat <<'NPXW' > "$NPX_WRAPPER"
#!/bin/bash
export LD_LIBRARY_PATH="/lib:/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
exec /mise/shims/npx "$@"
NPXW
chmod +x "$NPX_WRAPPER"

# Git Config — set name/email from host if available (for clean commits)
# We don't mount ~/.gitconfig to avoid exposing credentials/tokens,
# but we still want basic git identity for commits made inside the jail
if command -v git &>/dev/null; then
    # Extract name and email from host git config (passed via env from cli.py)
    if [ -n "$YOLO_GIT_NAME" ]; then
        git config --global user.name "$YOLO_GIT_NAME"
    fi
    if [ -n "$YOLO_GIT_EMAIL" ]; then
        git config --global user.email "$YOLO_GIT_EMAIL"
    fi
fi

# Copilot Config — use ~/.copilot directly (no XDG indirection)
COPILOT_CONFIG_DIR="$AGENT_HOME/.copilot"

# Clean up legacy XDG layout: migrate .config/.copilot -> .copilot
if [ -L "$COPILOT_CONFIG_DIR" ]; then
    # Remove stale symlink from previous entrypoint
    rm -f "$COPILOT_CONFIG_DIR"
fi
if [ -d "$AGENT_HOME/.config/.copilot" ] && [ ! -d "$COPILOT_CONFIG_DIR" ]; then
    mv "$AGENT_HOME/.config/.copilot" "$COPILOT_CONFIG_DIR"
fi

mkdir -p "$COPILOT_CONFIG_DIR"
if [ ! -f "$COPILOT_CONFIG_DIR/config.json" ]; then
    echo '{"yolo": true}' > "$COPILOT_CONFIG_DIR/config.json"
fi

# Merge host user-level gemini skills into jail (if mounted)
# Skills from host ~/.gemini/skills/ are made available inside the jail
# Clear the entire skills dir first so deleted skills don't persist in the cache
JAIL_SKILLS_DIR="$COPILOT_CONFIG_DIR/skills"
rm -rf "$JAIL_SKILLS_DIR"
mkdir -p "$JAIL_SKILLS_DIR"

if [ -n "$YOLO_HOST_GEMINI_SKILLS" ] && [ -d "$YOLO_HOST_GEMINI_SKILLS" ]; then
    # Sync host skills into jail (preserving structure, following symlinks)
    for skill_dir in "$YOLO_HOST_GEMINI_SKILLS"/*; do
        if [ -d "$skill_dir" ]; then
            cp -rL "$skill_dir" "$JAIL_SKILLS_DIR/"
        fi
    done
fi

# Workspace skills at /workspace/.copilot/skills/ are also synced if they exist
# Workspace skills take precedence over user-level skills
if [ -d "/workspace/.copilot/skills" ]; then
    for skill_dir in /workspace/.copilot/skills/*; do
        if [ -d "$skill_dir" ]; then
            skill_name=$(basename "$skill_dir")
            # Overwrite any user-level skill with same name
            rm -rf "$JAIL_SKILLS_DIR/$skill_name"
            cp -rL "$skill_dir" "$JAIL_SKILLS_DIR/"
        fi
    done
fi

python3 -c "
import json, os

config_dir = '$COPILOT_CONFIG_DIR'
home = os.environ['HOME']
npm_bin = os.path.join(os.environ.get('NPM_CONFIG_PREFIX', os.path.join(home, '.npm-global')), 'bin')
mcp_wrappers_bin = os.path.join(home, '.local/bin/mcp-wrappers')

# Write MCP Config
mcp_path = os.path.join(config_dir, 'mcp-config.json')
mcp_config = {
    'mcpServers': {
        'chrome-devtools': {
            'command': os.path.join(mcp_wrappers_bin, 'node'),
            'args': [
                os.path.join(npm_bin, 'chrome-devtools-mcp'),
                '--headless',
                '--isolated',
                '--executablePath', '/usr/bin/chromium',
                '--chrome-arg=--no-sandbox',
                '--chrome-arg=--disable-dev-shm-usage',
                '--chrome-arg=--disable-setuid-sandbox',
                '--chrome-arg=--disable-gpu',
                '--chrome-arg=--disable-software-rasterizer',
            ]
        },
        'sequential-thinking': {
            'command': os.path.join(mcp_wrappers_bin, 'node'),
            'args': [os.path.join(npm_bin, 'mcp-server-sequential-thinking')]
        }
    }
}
with open(mcp_path, 'w') as f:
    json.dump(mcp_config, f, indent=2)

# Write LSP Config
lsp_path = os.path.join(config_dir, 'lsp-config.json')
lsp_config = {
    'lspServers': {
        'python': {
            'command': os.path.join(npm_bin, 'pyright-langserver'),
            'args': ['--stdio'],
            'fileExtensions': {
                '.py': 'python',
                '.pyi': 'python'
            }
        },
        'typescript': {
            'command': os.path.join(npm_bin, 'typescript-language-server'),
            'args': ['--stdio'],
            'fileExtensions': {
                '.ts': 'typescript',
                '.tsx': 'typescriptreact',
                '.js': 'javascript',
                '.jsx': 'javascriptreact'
            }
        }
    }
}
with open(lsp_path, 'w') as f:
    json.dump(lsp_config, f, indent=2)
"

# Gemini Config with MCP Servers
GEMINI_CONFIG_DIR="$AGENT_HOME/.gemini"
mkdir -p "$GEMINI_CONFIG_DIR"
python3 -c "
import json, os, sys

config_path = '$GEMINI_CONFIG_DIR/settings.json'

home = os.environ['HOME']
npm_bin = os.path.join(os.environ.get('NPM_CONFIG_PREFIX', os.path.join(home, '.npm-global')), 'bin')
go_bin = os.path.join(os.environ.get('GOPATH', os.path.join(home, 'go')), 'bin')
mcp_wrappers_bin = os.path.join(home, '.local/bin/mcp-wrappers')

default_config = {
    'security': {'approvalMode': 'yolo', 'enablePermanentToolApproval': True},
    'mcpServers': {
        'chrome-devtools': {
            'command': os.path.join(mcp_wrappers_bin, 'node'),
            'args': [
                os.path.join(npm_bin, 'chrome-devtools-mcp'),
                '--headless',
                '--isolated',
                '--executablePath', '/usr/bin/chromium',
                '--chrome-arg=--no-sandbox',
                '--chrome-arg=--disable-dev-shm-usage',
                '--chrome-arg=--disable-setuid-sandbox',
                '--chrome-arg=--disable-gpu',
                '--chrome-arg=--disable-software-rasterizer',
            ]
        },
        'sequential-thinking': {
            'command': os.path.join(mcp_wrappers_bin, 'node'),
            'args': [os.path.join(npm_bin, 'mcp-server-sequential-thinking')]
        },
        'python-lsp': {
            'command': os.path.join(go_bin, 'mcp-language-server'),
            'args': ['-lsp', 'pyright-langserver', '-workspace', '/workspace', '--', '--stdio']
        },
        'typescript-lsp': {
            'command': os.path.join(go_bin, 'mcp-language-server'),
            'args': ['-lsp', 'typescript-language-server', '-workspace', '/workspace', '--', '--stdio']
        }
    }
}

try:
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            try:
                current = json.load(f)
            except json.JSONDecodeError:
                current = {}
        
        if 'mcpServers' not in current:
            current['mcpServers'] = {}
        
        current['mcpServers'].update(default_config['mcpServers'])
        
        if 'security' not in current:
            current['security'] = {}
        
        if 'approvalMode' not in current['security']:
            current['security']['approvalMode'] = 'yolo'
        if 'enablePermanentToolApproval' not in current['security']:
            current['security']['enablePermanentToolApproval'] = True
            
        final_config = current
    else:
        final_config = default_config

    with open(config_path, 'w') as f:
        json.dump(final_config, f, indent=2)
except Exception as e:
    sys.stderr.write(f'Error configuring Gemini MCP: {e}\\n')
"

# 7. Ensure PATH has shims, npm-global, go bins (already set by bashrc, but also here for safety)
export PATH="$SHIM_DIR:$NPM_CONFIG_PREFIX/bin:$GOPATH/bin:/bin:/usr/bin"
eval "$(mise activate bash)"

# 8. AGENTS.md is generated host-side by cli.py and mounted per-workspace
#    (no longer generated here — avoids shared-home conflicts between jails)

# 9. Run the startup command passed from Justfile
# Bootstrap is handled by cli.py's setup_script (with YOLO_BYPASS_SHIMS=1)
exec bash --rcfile "$BASHRC" -c "$@"
