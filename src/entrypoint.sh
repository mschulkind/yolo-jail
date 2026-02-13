#!/bin/bash
# YOLO Jail Entrypoint Script

# 1. Create a writable directory for dynamic shims
SHIM_DIR="/tmp/yolo-shims"
mkdir -p "$SHIM_DIR"

# 2. Default blocked tools
DEFAULT_BLOCKED="grep find"
BLOCKED_TOOLS="$DEFAULT_BLOCKED"

# 3. Read project-specific blocked tools from yolo-jail.toml if it exists
if [ -f "/workspace/yolo-jail.toml" ]; then
    # Simple extraction of blocked_tools array using sed (avoiding extra deps)
    PROJECT_BLOCKED=$(sed -n '/blocked_tools *= *\[/,/\]/p' /workspace/yolo-jail.toml | tr -d '[]", ' | grep -v "blocked_tools=")
    BLOCKED_TOOLS="$BLOCKED_TOOLS $PROJECT_BLOCKED"
fi

# 4. Generate shims
for tool in $BLOCKED_TOOLS; do
    SHIM_PATH="$SHIM_DIR/$tool"
    
    # Skip if we already have a specialized shim or if the tool is essential for startup
    if [ "$tool" == "grep" ] || [ "$tool" == "find" ]; then
        # Use our "smart" shims that allow script usage but block interactive use
        cat <<EOF > "$SHIM_PATH"
#!/bin/sh
if [ -t 1 ] && [ -z "\$YOLO_BYPASS_SHIMS" ]; then
  echo "Error: '\$0' is disabled for direct use. Use modern alternatives (rg, fd) instead." >&2
  exit 127
fi
exec /bin/$tool "\$@"
EOF
    else
        # Hard block for other tools
        cat <<EOF > "$SHIM_PATH"
#!/bin/sh
echo "Error: tool '$tool' is explicitly blocked in this project's yolo-jail.toml." >&2
exit 127
EOF
    fi
    chmod +x "$SHIM_PATH"
done

# 5. Place shims first in PATH
export PATH="$SHIM_DIR:$PATH"

# 6. Run the startup command passed from Justfile
exec bash -c "$@"
