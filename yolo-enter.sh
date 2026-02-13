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

cd "$REPO_ROOT"

# Default to 'run' command if the first argument isn't a known subcommand or help
if [ "$1" == "init" ] || [ "$1" == "--help" ]; then
    exec uv run src/cli.py "$@"
else
    exec uv run src/cli.py run "$@"
fi
