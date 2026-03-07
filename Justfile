# Container runtime (podman or docker)
runtime := env("YOLO_RUNTIME", "podman")

default:
    @just --list

# Build the container image using Nix
build:
    nix --extra-experimental-features 'nix-command flakes' build .#dockerImage

# Build and load the image into the container runtime
load: build
    {{runtime}} load < result

# Run all tests
test:
    uv run pytest tests/

# Run linter
lint:
    uv run ruff check .

# Format code
format:
    uv run ruff check --fix .
    uv run ruff format .

# All quality checks
check: format lint test

# Clean up build artifacts
clean:
    rm -f result

# Push bookmarks to remotes
push:
    jj git push --bookmark main --remote public
    jj git push --bookmark main --bookmark dev --bookmark staging --remote private

# Pre-promote quality gate
prepromote:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Pre-promote quality checks ==="

    # Staging has changes
    if [ -z "$(jj diff -r staging --stat 2>/dev/null)" ]; then
        echo "FAIL: staging has no changes to promote."
        exit 1
    fi

    # Description is proper (not placeholder)
    DESC="$(jj log -r staging --no-graph -T description 2>/dev/null)"
    if [ -z "$DESC" ] || echo "$DESC" | grep -qi "^staging:"; then
        echo "FAIL: staging description must be a proper release description."
        exit 1
    fi

    # Run project quality gates
    just check
    echo "=== All pre-promote checks passed ==="

# Promote staging to main
promote: prepromote
    #!/usr/bin/env bash
    set -euo pipefail
    DESC="$(jj log -r staging --no-graph -T description 2>/dev/null)"
    echo "--- Promoting staging to main ---"
    echo "Description: $DESC"
    jj bookmark set main -r staging
    jj new --insert-after main --insert-before dev
    jj bookmark set staging -r @
    jj desc -m "Staging: accumulating changes for next public release"
    jj edit dev
    just push
    echo "Promote complete."
