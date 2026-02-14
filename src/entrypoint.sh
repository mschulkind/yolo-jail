#!/bin/bash
# YOLO Jail Entrypoint Script

# 1. Create a writable directory for dynamic shims in the persistent store
SHIM_DIR="$HOME/.yolo-shims"
rm -rf "$SHIM_DIR"
mkdir -p "$SHIM_DIR"

# 2. Default blocked tools
DEFAULT_BLOCKED="grep find"
BLOCKED_TOOLS="$DEFAULT_BLOCKED"

# 3. Read blocked tools from environment variable (injected by Python CLI)
if [ -n "$YOLO_BLOCK_CONFIG" ]; then
    SHIM_DIR="$SHIM_DIR" python3 -c "
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
        
        content = ''
        if name in ['grep', 'find']:
            content = f'''#!/bin/sh
if [ -t 1 ] && [ -z \"\$YOLO_BYPASS_SHIMS\" ]; then
  echo \"{msg}\" >&2
  [ -n \"{sug}\" ] && echo \"Suggestion: {sug}\" >&2
  exit 127
fi
exec /bin/{name} \"\$@\"
'''
        else:
            content = f'''#!/bin/sh
echo \"{msg}\" >&2
[ -n \"{sug}\" ] && echo \"Suggestion: {sug}\" >&2
exit 127
'''
        
        with open(shim_path, 'w') as f:
            f.write(content)
        
        st = os.stat(shim_path)
        os.chmod(shim_path, st.st_mode | stat.S_IEXEC)
        
except Exception as e:
    sys.stderr.write(f'Error generating shims: {e}\\n')
"
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

# Initialize font cache for Chromium
fc-cache -f >/dev/null 2>&1

# Agent-friendly defaults (no pagers, no line numbers)
export PAGER=cat
export BAT_PAGER=""
export BAT_STYLE="plain"
export GIT_PAGER=cat
export EDITOR=nvim

# Aliases
alias ls='ls --color=auto'
alias ll='ls -alF'
alias grep='grep --color=auto'
alias gemini='gemini --yolo'
alias copilot='copilot --yolo'
alias vi='nvim'
alias vim='nvim'
alias bat='bat --style=plain --paging=never'
EOF

# 6. Bootstrap Default Agent Configs (YOLO Mode)
AGENT_HOME="${JAIL_HOME:-/home/agent}"

# Ensure npm global bin is in PATH
export NPM_CONFIG_PREFIX="$AGENT_HOME/.npm-global"
export PATH="$NPM_CONFIG_PREFIX/bin:$AGENT_HOME/go/bin:$PATH"

# Create a bootstrap script that will run AFTER mise is ready
BOOTSTRAP_SCRIPT="$AGENT_HOME/.yolo-bootstrap.sh"
cat <<'EOF' > "$BOOTSTRAP_SCRIPT"
#!/bin/bash
export NPM_CONFIG_PREFIX="$HOME/.npm-global"
export GOPATH="$HOME/go"
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
CHROME_WRAPPER="$AGENT_HOME/.local/bin/chrome-devtools-mcp-wrapper"
mkdir -p "$(dirname "$CHROME_WRAPPER")"
cat <<'WRAPPER' > "$CHROME_WRAPPER"
#!/bin/bash
CHROME_PORT=9222
CHROME_URL="http://127.0.0.1:$CHROME_PORT"

# Start Chromium if not already running
if ! curl -s "$CHROME_URL/json/version" >/dev/null 2>&1; then
    /usr/bin/chromium \
        --headless \
        --no-sandbox \
        --disable-dev-shm-usage \
        --disable-gpu \
        --disable-software-rasterizer \
        --disable-setuid-sandbox \
        --remote-debugging-address=127.0.0.1 \
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

exec node /home/agent/.npm-global/bin/chrome-devtools-mcp \
    --browser-url "$CHROME_URL" \
    "$@"
WRAPPER
chmod +x "$CHROME_WRAPPER"

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

python3 -c "
import json, os

config_dir = '$COPILOT_CONFIG_DIR'

# Write MCP Config
mcp_path = os.path.join(config_dir, 'mcp-config.json')
mcp_config = {
    'mcpServers': {
        'chrome-devtools': {
            'command': '/home/agent/.local/bin/chrome-devtools-mcp-wrapper'
        },
        'sequential-thinking': {
            'command': 'node',
            'args': ['/home/agent/.npm-global/bin/mcp-server-sequential-thinking']
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
            'command': '/home/agent/.npm-global/bin/pyright-langserver',
            'args': ['--stdio'],
            'fileExtensions': ['py']
        },
        'typescript': {
            'command': '/home/agent/.npm-global/bin/typescript-language-server',
            'args': ['--stdio'],
            'fileExtensions': ['ts', 'tsx', 'js', 'jsx']
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
default_config = {
    'security': {'approvalMode': 'yolo', 'enablePermanentToolApproval': True},
    'mcpServers': {
        'chrome-devtools': {
            'command': '/home/agent/.local/bin/chrome-devtools-mcp-wrapper'
        },
        'sequential-thinking': {
            'command': 'node',
            'args': ['/home/agent/.npm-global/bin/mcp-server-sequential-thinking']
        },
        'python-lsp': {
            'command': '/home/agent/go/bin/mcp-language-server',
            'args': ['-lsp', 'pyright-langserver', '-workspace', '/workspace', '--', '--stdio']
        },
        'typescript-lsp': {
            'command': '/home/agent/go/bin/mcp-language-server',
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

# 7. Place shims first in PATH
export PATH="$SHIM_DIR:$PATH"

# 8. Generate dynamic AGENTS.md in workspace (prepend jail info, preserve project content)
JAIL_AGENTS="/workspace/AGENTS.md"
HOST_IP=$(ip route | awk '/default/ {print $3}' 2>/dev/null || echo "unknown")
python3 -c "
import json, os

host_dir = os.environ.get('YOLO_HOST_DIR', 'unknown')
host_ip = '$HOST_IP'
block_config = os.environ.get('YOLO_BLOCK_CONFIG', '[]')
mounts_json = os.environ.get('YOLO_MOUNTS', '[]')

try:
    blocked = json.loads(block_config)
except Exception:
    blocked = []

try:
    mounts = json.loads(mounts_json)
except Exception:
    mounts = []

# Preserve existing project AGENTS.md content (skip any previous jail section)
MARKER = '<!-- YOLO-JAIL-END -->'
project_content = ''
agents_path = '$JAIL_AGENTS'
if os.path.exists(agents_path):
    with open(agents_path, 'r') as f:
        existing = f.read()
    if MARKER in existing:
        project_content = existing.split(MARKER, 1)[1].strip()
    else:
        project_content = existing.strip()

lines = []
lines.append('<!-- YOLO-JAIL-START (auto-generated, do not edit above YOLO-JAIL-END) -->')
lines.append('# YOLO Jail Environment')
lines.append('')
lines.append('You are running inside a YOLO Jail — a sandboxed Docker container.')
lines.append('')
lines.append('## Environment')
lines.append('')
lines.append(f'- **Workspace**: \`/workspace\` (mounted from host \`{host_dir}\`)')
lines.append(f'- **Host IP** (from container): \`{host_ip}\`')
lines.append(f'- **Home Directory**: \`/home/agent\` (persistent across sessions)')
lines.append('- **OS**: NixOS-based minimal container (no systemd, no sudo)')
lines.append('- **Network**: Bridge mode by default. Use host IP above to reach host services.')
lines.append('')
lines.append('## Available Tools')
lines.append('')
lines.append('Standard CLI tools: git, rg (ripgrep), fd, bat, jq, nvim, curl, wget, strace, gh')
lines.append('Runtimes: Node.js 22, Python 3.13, Go (managed by mise)')
lines.append('MCP Servers: chrome-devtools (headless Chromium), sequential-thinking')
lines.append('')

if blocked:
    lines.append('## Blocked Tools')
    lines.append('')
    lines.append('The following tools are blocked or shimmed in this project:')
    lines.append('')
    for tool in blocked:
        name = tool.get('name', str(tool))
        msg = tool.get('message', '')
        sug = tool.get('suggestion', '')
        entry = f'- \`{name}\`'
        if msg:
            entry += f': {msg}'
        if sug:
            entry += f' Use \`{sug}\` instead.'
        lines.append(entry)
    lines.append('')

if mounts:
    lines.append('## Additional Context Mounts (read-only)')
    lines.append('')
    for m in mounts:
        host_path, container_path = m.split(':', 1) if ':' in m else (m, m)
        lines.append(f'- \`{container_path}\` (from host \`{host_path}\`)')
    lines.append('')

lines.append('## Limitations')
lines.append('')
lines.append('- **No internet restrictions** but no host credentials (no ~/.ssh, no ~/.gitconfig).')
lines.append('- **No pagers**: PAGER=cat, GIT_PAGER=cat. Do not pipe to less/more.')
lines.append('- **Read-only mounts**: Context mounts under \`/ctx/\` are read-only.')
lines.append('- **No sudo/root**: You run as a mapped host user with no privilege escalation.')
lines.append('- Authenticate with \`gh auth login\` if you need GitHub access.')
lines.append('')
lines.append(MARKER)

if project_content:
    lines.append('')
    lines.append(project_content)

with open(agents_path, 'w') as f:
    f.write('\n'.join(lines) + '\n')
"

# 9. Run the startup command passed from Justfile
# We bypass shims for mise and startup tasks
YOLO_BYPASS_SHIMS=1 exec bash --rcfile "$BASHRC" -c "$@"