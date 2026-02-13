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

python3 -c "
import json, os

# Copilot Config Locations
config_dirs = ['$AGENT_HOME/.copilot', '$AGENT_HOME/.config/.copilot']

for d in config_dirs:
    os.makedirs(d, exist_ok=True)
    
    # Write MCP Config
    mcp_path = os.path.join(d, 'mcp-config.json')
    mcp_config = {
        'mcpServers': {
            'chrome-devtools': {
                'command': '/home/agent/.npm-global/bin/chrome-devtools-mcp',
                'args': ['--headless', '--no-sandbox', '--executable-path', '/usr/bin/chromium', '--disable-dev-shm-usage', '--disable-gpu']
            },
            'sequential-thinking': {
                'command': '/home/agent/.npm-global/bin/mcp-server-sequential-thinking',
                'args': []
            }
        }
    }
    with open(mcp_path, 'w') as f:
        json.dump(mcp_config, f, indent=2)

    # Write LSP Config
    lsp_path = os.path.join(d, 'lsp-config.json')
    lsp_config = {
        'lspServers': {
            'python': {
                'command': '/home/agent/.npm-global/bin/pyright-langserver',
                'args': ['--stdio']
            },
            'typescript': {
                'command': '/home/agent/.npm-global/bin/typescript-language-server',
                'args': ['--stdio']
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
            'command': '/home/agent/.npm-global/bin/chrome-devtools-mcp',
            'args': ['--headless', '--no-sandbox', '--executable-path', '/usr/bin/chromium', '--disable-dev-shm-usage', '--disable-gpu']
        },
        'sequential-thinking': {
            'command': '/home/agent/.npm-global/bin/mcp-server-sequential-thinking',
            'args': []
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

# 8. Run the startup command passed from Justfile
# We bypass shims for mise and startup tasks
YOLO_BYPASS_SHIMS=1 exec bash --rcfile "$BASHRC" -c "$@"