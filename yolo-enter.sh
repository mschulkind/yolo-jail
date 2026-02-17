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

# If inside tmux and in an interactive shell, set visual jail indicators
# Skip tmux modifications for non-interactive runs (e.g., tests, CI) to avoid side effects
if [ -n "$TMUX" ] && [ -t 0 ]; then
    _JAIL_DIR="$(basename "$PWD")"
    _TMUX_PANE="$TMUX_PANE"  # Save current pane ID for targeted cleanup

    # Save old state using proper format for restoration
    _OLD_BORDER=$(tmux show-option -pt "$_TMUX_PANE" pane-border-style 2>/dev/null)
    _OLD_ACTIVE=$(tmux show-option -pt "$_TMUX_PANE" pane-active-border-style 2>/dev/null)
    _OLD_BSTATUS=$(tmux show-option -pt "$_TMUX_PANE" pane-border-status 2>/dev/null)
    _OLD_BFORMAT=$(tmux show-option -pt "$_TMUX_PANE" pane-border-format 2>/dev/null)

    # Set jail indicators — pane-level options target only this pane
    tmux set-option -pt "$_TMUX_PANE" pane-border-style "fg=red,bold" 2>/dev/null
    tmux set-option -pt "$_TMUX_PANE" pane-active-border-style "fg=red,bold" 2>/dev/null
    tmux set-option -pt "$_TMUX_PANE" pane-border-status bottom 2>/dev/null
    tmux set-option -pt "$_TMUX_PANE" pane-border-format " 🔒 JAIL $_JAIL_DIR " 2>/dev/null

    # Set tmux window name to show JAIL in status bar
    _OLD_WINDOW_NAME=$(tmux display-message -p '#{window_name}' 2>/dev/null)
    _OLD_AUTO_RENAME=$(tmux show-window-option -v automatic-rename 2>/dev/null)
    tmux set-window-option automatic-rename off 2>/dev/null
    tmux rename-window "JAIL $_JAIL_DIR" 2>/dev/null

    _restore_border() {
        # Restore all saved options
        [ -n "$_OLD_BORDER" ] && eval "tmux $_OLD_BORDER -pt '$_TMUX_PANE'" 2>/dev/null || tmux set-option -put "$_TMUX_PANE" pane-border-style 2>/dev/null
        [ -n "$_OLD_ACTIVE" ] && eval "tmux $_OLD_ACTIVE -pt '$_TMUX_PANE'" 2>/dev/null || tmux set-option -put "$_TMUX_PANE" pane-active-border-style 2>/dev/null
        [ -n "$_OLD_BSTATUS" ] && eval "tmux $_OLD_BSTATUS -pt '$_TMUX_PANE'" 2>/dev/null || tmux set-option -put "$_TMUX_PANE" pane-border-status 2>/dev/null
        [ -n "$_OLD_BFORMAT" ] && eval "tmux $_OLD_BFORMAT -pt '$_TMUX_PANE'" 2>/dev/null || tmux set-option -put "$_TMUX_PANE" pane-border-format 2>/dev/null
        # Restore window name
        if [ -n "$_OLD_WINDOW_NAME" ]; then
            tmux rename-window "$_OLD_WINDOW_NAME" 2>/dev/null
        fi
        if [ "$_OLD_AUTO_RENAME" = "on" ]; then
            tmux set-window-option automatic-rename on 2>/dev/null
        fi
    }
    trap _restore_border EXIT
fi

# Run the CLI using uv, pointing to the jail project while staying in the user's current directory
_run_jail() { uv run --project "$REPO_ROOT" "$REPO_ROOT/src/cli.py" "$@"; }

if [ -z "$1" ]; then
    # No arguments: start an interactive shell
    _run_jail run
elif [ "$1" == "init" ] || [ "$1" == "init-user-config" ] || [ "$1" == "run" ] || [ "$1" == "ps" ] || [ "$1" == "--help" ] || [ "$1" == "-h" ]; then
    # Explicit subcommands or help
    _run_jail "$@"
elif [ "$1" == "--new" ]; then
    # Force new container: yolo --new -- <command>
    shift
    if [ "$1" == "--" ]; then
        shift
        _run_jail run --new -- "$@"
    else
        _run_jail run --new "$@"
    fi
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
