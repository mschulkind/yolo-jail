# Container runtime (podman or docker)
runtime := env("YOLO_RUNTIME", "podman")

default:
    @just --list

# Install editable package and patch finder for relocatable paths (host + jail)
setup:
    #!/usr/bin/env bash
    set -euo pipefail

    # Editable install into mise's Python
    uv pip install -e .

    # Locate the generated finder module
    SITE_PACKAGES="$(python3 -c 'import site; print(site.getsitepackages()[0])')"
    FINDER=$(ls "$SITE_PACKAGES"/__editable___yolo_jail*_finder.py 2>/dev/null | head -1)

    if [ -z "$FINDER" ] || [ ! -f "$FINDER" ]; then
        echo "ERROR: editable finder not found in $SITE_PACKAGES" >&2
        exit 1
    fi

    REPO_ROOT="$(pwd)"

    # Patch the static MAPPING to resolve dynamically via YOLO_REPO_ROOT
    python3 - "$FINDER" "$REPO_ROOT" <<'PYEOF'
    import re, sys
    finder_path, repo_root = sys.argv[1], sys.argv[2]
    with open(finder_path) as f:
        content = f.read()
    m = re.search(r"^MAPPING:.*$", content, re.MULTILINE)
    if not m:
        print("WARN: MAPPING line not found, already patched?", file=sys.stderr)
        sys.exit(0)
    new_block = (
        "import os as _os\n"
        f"_YOLO_ROOT = _os.environ.get('YOLO_REPO_ROOT', '{repo_root}')\n"
        "MAPPING: dict[str, str] = {'src': _os.path.join(_YOLO_ROOT, 'src')}"
    )
    content = content[:m.start()] + new_block + content[m.end():]
    with open(finder_path, "w") as f:
        f.write(content)
    PYEOF

    echo "Patched $FINDER"
    echo "  Host fallback: $REPO_ROOT"
    echo "  Jail: uses \$YOLO_REPO_ROOT (/opt/yolo-jail)"

# Build the Python package (version derived from git tags via setuptools-scm)
build:
    rm -rf dist/
    uv build

# Install yolo as a standalone tool (decoupled from source tree)
install: build
    uv tool install --force "$(ls -1 dist/*.whl | head -1)"

# Installs:
#   1. yolo CLI (via uv tool install)
#   2. Claude OAuth token refresher (systemd --user timer, if on Linux)
# Safe to re-run — idempotent.
# Build and install everything needed for yolo-jail on this host
deploy: install
    #!/usr/bin/env bash
    set -euo pipefail

    # --- Claude token refresher (systemd --user timer) ---
    # Skipped when `claude` isn't on PATH — no point running a refresher for a
    # tool the user hasn't installed.  They can run `just deploy` again later
    # after installing Claude Code, or drop `claude_token_refresher: false`
    # into ~/.config/yolo-jail/config.jsonc to silence `yolo check` about it.
    if ! command -v claude >/dev/null 2>&1; then
        echo "⚠ claude not found on PATH — skipping Claude token refresher install"
        echo "  (install Claude Code and re-run \`just deploy\` to enable it,"
        echo "   or set \`claude_token_refresher: false\` in your yolo config)"
    elif ! command -v systemctl >/dev/null 2>&1; then
        echo "⚠ systemctl not found — skipping token refresher (not a systemd host)"
    else
        REPO_ROOT="$(pwd)"
        SCRIPT="$REPO_ROOT/scripts/claude-token-refresher.py"
        if [ ! -x "$SCRIPT" ]; then
            echo "ERROR: $SCRIPT not found or not executable" >&2
            exit 1
        fi

        SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_DIR"

        # Substitute the repo path into the service template so ExecStart
        # points at the right checkout.  The template uses the literal
        # "%h/code/yolo-jail" marker so it stays readable in-repo.
        sed "s|%h/code/yolo-jail|$REPO_ROOT|g" \
            scripts/claude-token-refresher.service \
            > "$SYSTEMD_DIR/claude-token-refresher.service"
        cp scripts/claude-token-refresher.timer "$SYSTEMD_DIR/"

        systemctl --user daemon-reload
        systemctl --user enable --now claude-token-refresher.timer

        # Fire once now so the first refresh check runs without waiting for
        # OnBootSec.  Non-fatal — the timer retries on its own cadence.
        systemctl --user start claude-token-refresher.service || true

        echo "✓ claude-token-refresher installed at $SYSTEMD_DIR"
    fi

    echo "yolo-jail deployed. Verify: yolo check"

# Build the container image using Nix
build-image:
    nix --extra-experimental-features 'nix-command flakes' build .#dockerImage

# Build and load the image into the container runtime
load: build-image
    ./result | {{runtime}} load

# Run all tests
test:
    uv run --group dev python -m pytest tests/

# Run fast tests only (skip container integration tests)
test-fast:
    uv run --group dev python -m pytest tests/ -m "not slow"

# Run linter
lint:
    uv run ruff check .

# Lint without auto-fix (CI mode — fails on violations, doesn't modify files)
lint-ci:
    uv run ruff check .
    uv run ruff format --check .

# Format code
format:
    uv run ruff check --fix .
    uv run ruff format .

# Quality checks (interactive use)
check: format lint test-fast

# Pre-commit hook target (no formatting — just verify and test)
check-ci: lint-ci test-fast

# Full quality checks including container integration tests
check-all: format lint test

# Clean up build artifacts
clean:
    rm -f result
    rm -rf dist/ build/

