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

# Run the CLI using uv, pointing to the jail project while staying in the user's current directory
if [ "$1" == "init" ] || [ "$1" == "run" ] || [ "$1" == "--help" ]; then
    exec uv run --project "$REPO_ROOT" "$REPO_ROOT/src/cli.py" "$@"
else
    exec uv run --project "$REPO_ROOT" "$REPO_ROOT/src/cli.py" run "$@"
fi
