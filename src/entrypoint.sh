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

# Aliases
alias ls='ls --color=auto'
alias ll='ls -alF'
alias grep='grep --color=auto'
EOF

# 6. Bootstrap Default Agent Configs (YOLO Mode)
AGENT_HOME="${JAIL_HOME:-/home/agent}"

# Global Mise Config (to provide defaults if project has no mise.toml)
MISE_CONFIG_DIR="$AGENT_HOME/.config/mise"
if [ ! -f "$MISE_CONFIG_DIR/config.toml" ]; then
    mkdir -p "$MISE_CONFIG_DIR"
    cat <<EOF > "$MISE_CONFIG_DIR/config.toml"
[tools]
node = "system"
python = "system"
"npm:@google/gemini-cli" = "latest"
"npm:@github/copilot" = "latest"
EOF
fi

# Copilot Config
COPILOT_CONFIG_DIR="$AGENT_HOME/.config/.copilot"
if [ ! -f "$COPILOT_CONFIG_DIR/config.json" ]; then
    mkdir -p "$COPILOT_CONFIG_DIR"
    echo '{"yolo": true}' > "$COPILOT_CONFIG_DIR/config.json"
fi

# Gemini Config
GEMINI_CONFIG_DIR="$AGENT_HOME/.gemini"
if [ ! -f "$GEMINI_CONFIG_DIR/settings.json" ]; then
    mkdir -p "$GEMINI_CONFIG_DIR"
    echo '{"security": {"approvalMode": "yolo", "enablePermanentToolApproval": true}}' > "$GEMINI_CONFIG_DIR/settings.json"
fi

# 7. Place shims first in PATH
export PATH="$SHIM_DIR:$PATH"

# 8. Run the startup command passed from Justfile
# We bypass shims for mise and startup tasks
YOLO_BYPASS_SHIMS=1 exec bash --rcfile "$BASHRC" -c "$@"
