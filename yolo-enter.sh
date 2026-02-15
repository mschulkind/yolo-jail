#!/bin/bash
# YOLO Jail Global Entrypoint
# This script launches the Python CLI using uv

# Resolve the directory of the real script (handling symlinks)
SOURCE=${BASH_SOURCE[0]}
while [ -L "$SOURCE" ]; do 
  DIR=$( cd -P "$( dirname "$SOURCE" )" >/dev/null 2>&1 && pwd )
  SOURCE=$(readlink "$SOURCE")
  [[ $SOURCE != /* ]] && SOURCE=$DIR/$SOURCE 
done
REPO_ROOT=$( cd -P "$( dirname "$SOURCE" )" >/dev/null 2>&1 && pwd )

# If inside tmux, set visual jail indicators and restore on exit
if [ -n "$TMUX" ]; then
    _JAIL_DIR="$(basename "$PWD")"
    _TMUX_PANE="$TMUX_PANE"  # Save current pane ID for targeted cleanup

    # Save old state
    _OLD_BORDER=$(tmux show-option -pt "$_TMUX_PANE" pane-border-style 2>/dev/null | sed 's/^pane-border-style //')
    _OLD_ACTIVE=$(tmux show-option -pt "$_TMUX_PANE" pane-active-border-style 2>/dev/null | sed 's/^pane-active-border-style //')
    _OLD_BSTATUS=$(tmux show-option -pt "$_TMUX_PANE" pane-border-status 2>/dev/null | sed 's/^pane-border-status //')
    _OLD_BFORMAT=$(tmux show-option -pt "$_TMUX_PANE" pane-border-format 2>/dev/null | sed 's/^pane-border-format //')

    # Set jail indicators — pane-level options target only this pane
    tmux set-option -pt "$_TMUX_PANE" pane-border-style "fg=red,bold" 2>/dev/null
    tmux set-option -pt "$_TMUX_PANE" pane-active-border-style "fg=red,bold" 2>/dev/null
    tmux set-option -pt "$_TMUX_PANE" pane-border-status bottom 2>/dev/null
    tmux set-option -pt "$_TMUX_PANE" pane-border-format " 🔒 JAIL $_JAIL_DIR " 2>/dev/null

    # Set window name — disable auto-rename so it sticks
    tmux set-option -w automatic-rename off 2>/dev/null
    tmux rename-window "JAIL $_JAIL_DIR" 2>/dev/null

    _restore_border() {
        # Restore pane options
        for opt in pane-border-style pane-active-border-style pane-border-status pane-border-format; do
            tmux set-option -put "$_TMUX_PANE" "$opt" 2>/dev/null
        done
        # Re-enable auto-rename so tmux names the window based on the running process
        tmux set-option -w automatic-rename on 2>/dev/null
    }
    trap _restore_border EXIT
fi

# Run the CLI using uv, pointing to the jail project while staying in the user's current directory
_run_jail() { uv run --project "$REPO_ROOT" "$REPO_ROOT/src/cli.py" "$@"; }

if [ -z "$1" ]; then
    # No arguments: start an interactive shell
    _run_jail run
elif [ "$1" == "init" ] || [ "$1" == "init-user-config" ] || [ "$1" == "run" ] || [ "$1" == "--help" ] || [ "$1" == "-h" ]; then
    # Explicit subcommands or help
    _run_jail "$@"
elif [ "$1" == "--" ]; then
    # Treat everything after -- as the command to run in the jail
    shift
    _run_jail run -- "$@"
else
    # Rejection of the "old way" (implicit command execution)
    echo "Error: Unknown argument '$1'. " >&2
    echo "Usage:" >&2
    echo "  yolo              # Open interactive shell" >&2
    echo "  yolo init         # Initialize configuration" >&2
    echo "  yolo init-user-config # Initialize user-level defaults" >&2
    echo "  yolo -- <command> # Run command directly" >&2
    exit 1
fi
