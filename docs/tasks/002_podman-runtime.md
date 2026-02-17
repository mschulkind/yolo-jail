# Tasks: Podman Runtime Support

**RFC:** [002_podman-runtime.md](../plans/002_podman-runtime.md)

## Phase 1: Core Runtime Abstraction

- [x] Add `_runtime()` function to cli.py with env/config/auto-detect logic
- [x] Thread `runtime` parameter through all functions that call container commands
- [x] Replace hardcoded `"docker"` with runtime param in all container operations
- [x] Update Justfile to support podman

## Phase 2: Testing & Validation

- [x] Run existing tests (ensure nothing breaks)
- [x] Manual test: `YOLO_RUNTIME=podman yolo -- echo hello`
- [x] Manual test: `YOLO_RUNTIME=docker yolo -- echo hello`
- [x] Manual test: auto-detect (unset YOLO_RUNTIME)
- [x] Manual test: container reuse (exec into running container)
- [x] Manual test: `yolo ps`
- [x] Add parametrized multi-runtime integration tests (test_runtime.py)

## Phase 3: Rootless Podman-in-Podman

- [x] Add podman, nix, fuse-overlayfs, slirp4netns, shadow to jail image
- [x] Configure /etc/containers/* for inner podman (storage.conf, containers.conf, policy.json)
- [x] Add UID/GID mappings, capabilities, /dev/fuse in cli.py for podman
- [x] Verify rootless approach (no `--privileged`)
- [x] Inner containers use `--net=host` + `--cgroups=disabled`

## Phase 4: Nix Builds Inside Jail

- [x] Diagnose nix local vs daemon mode issue (`nixbld` group has no members)
- [x] Discover `NIX_REMOTE=daemon` forces host daemon delegation
- [x] Auto-mount `/nix/var/nix/daemon-socket` + `/nix/store:ro` when host has nix
- [x] Set `NIX_REMOTE=daemon` in container environment
- [x] Verify end-to-end: nix build with new packages inside jail succeeds

## Phase 5: Documentation

- [x] Update AGENTS.md with podman support, nested containers, nix-in-jail
- [x] Commit and push all changes
