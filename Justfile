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

# Installs the yolo CLI and primes the Claude OAuth broker state.
# Safe to re-run — idempotent.
deploy: install
    #!/usr/bin/env bash
    set -euo pipefail

    # --- Retire pre-broker Claude token refresher install ---
    # The refresher daemon was removed — the broker refreshes on demand
    # now (see docs/claude-token-logouts.md).  If an older `just deploy`
    # installed the systemd --user timer, tear it down so it doesn't run
    # against a binary that no longer exists.
    if command -v systemctl >/dev/null 2>&1; then
        for unit in claude-token-refresher.timer claude-token-refresher.service; do
            if systemctl --user is-enabled "$unit" >/dev/null 2>&1 \
              || systemctl --user is-active "$unit" >/dev/null 2>&1; then
                systemctl --user disable --now "$unit" 2>/dev/null || true
                echo "  retired legacy $unit"
            fi
        done
        rm -f "$HOME/.config/systemd/user/claude-token-refresher.service"
        rm -f "$HOME/.config/systemd/user/claude-token-refresher.timer"
        systemctl --user daemon-reload 2>/dev/null || true
    fi

    # --- Claude OAuth broker loophole (bundled) ---
    # The manifest ships inside the yolo-jail wheel under
    # src/bundled_loopholes/claude-oauth-broker/ — the loader finds it
    # automatically whenever yolo-jail is installed.  The loophole's
    # ``requires.command_on_path: claude`` predicate gates activation,
    # so there's no separate "is Claude installed" check here.
    #
    # The only host-install step: pre-generate the CA + leaf into the
    # writable state dir so jails have something to trust on first
    # boot.  Also retires any pre-bundled-era install artifacts.
    if ! command -v openssl >/dev/null 2>&1; then
        echo "⚠ openssl not found — skipping claude-oauth-broker state init"
    else
        BROKER_BIN="$(command -v yolo-claude-oauth-broker-host || true)"
        if [ -z "$BROKER_BIN" ]; then
            echo "ERROR: yolo-claude-oauth-broker-host not on PATH after install" >&2
            exit 1
        fi

        # Retire stale copies of the manifest from pre-bundled installs.
        rm -rf "$HOME/.local/share/yolo-jail/modules/claude-oauth-broker"
        if [ -d "$HOME/.local/share/yolo-jail/loopholes/claude-oauth-broker" ]; then
            # Move any generated state into the new state dir before
            # removing the legacy copy (the manifest lives in the wheel
            # now, but CA/leaf files shouldn't be lost).
            STATE_DIR="$HOME/.local/share/yolo-jail/state/claude-oauth-broker"
            mkdir -p "$STATE_DIR"
            for f in ca.crt ca.key server.crt server.key refresh.lock; do
                src_f="$HOME/.local/share/yolo-jail/loopholes/claude-oauth-broker/$f"
                [ -f "$src_f" ] && mv "$src_f" "$STATE_DIR/$f" 2>/dev/null || true
            done
            rm -rf "$HOME/.local/share/yolo-jail/loopholes/claude-oauth-broker"
            echo "  migrated legacy loopholes/claude-oauth-broker → bundled + state split"
        fi
        # Retire the pre-split systemd unit if present.
        if command -v systemctl >/dev/null 2>&1; then
            if systemctl --user is-enabled claude-oauth-broker.service >/dev/null 2>&1; then
                systemctl --user disable --now claude-oauth-broker.service 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/claude-oauth-broker.service"
                systemctl --user daemon-reload
                echo "  retired pre-split claude-oauth-broker.service"
            fi
        fi

        # Generate CA + leaf in the state dir (idempotent).
        "$BROKER_BIN" --init-ca >/dev/null

        echo "✓ claude-oauth-broker state primed at $HOME/.local/share/yolo-jail/state/claude-oauth-broker"
        echo "  manifest is bundled in the wheel; loophole activates automatically when Claude is on PATH"
    fi

    echo "yolo-jail deployed. Verify: yolo loopholes list"

# Build the container image using Nix
build-image:
    nix --extra-experimental-features 'nix-command flakes' build .#dockerImage

# Build the minimal image variant used by CI integration (no chromium,
# gcc toolchain, nested-podman, or debug tools — ~1.6–2 GB smaller).
# Contains everything tests/test_jail.py exercises but nothing more.
build-image-minimal:
    nix --extra-experimental-features 'nix-command flakes' build .#dockerImageMinimal

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

