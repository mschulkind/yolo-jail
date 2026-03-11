# Cross-Project Improvement Roadmap

**Date**: 2026-03-08
**Source**: Survey of all projects in `~/code/` and `~/projects/` that use yolo-jail

## Executive Summary

A survey of 16+ projects using yolo-jail reveals recurring friction around **Python venv paths**, **CLI shebang breakage**, and **inside/outside consistency**. Two projects (`session_manager`, `pi`) carry manual shebang-fixing hacks in their Justfiles. Three projects (`vantage`, `copilot-viewer`, `layman`) avoid the problem by using `uv tool install` for deployment. The root cause is the jail mounting workspaces at `/workspace` instead of their original host path, causing absolute paths baked into `.venv/bin/` scripts and `pyvenv.cfg` to break.

This document catalogs every pattern, workaround, and pain point found, then proposes a phased roadmap to eliminate them.

---

## Inventory: How Projects Use the Jail

### Jail Configurations (`yolo-jail.jsonc`)

| Project | Runtime | Packages | Blocked Tools | Network | Mounts | mise_tools |
|---------|---------|----------|---------------|---------|--------|------------|
| **copilot-viewer** | podman | — | — | bridge, 8610:8600 | 10+ .yolo dirs | — |
| **vantage** | default | — | grep, find | bridge | — | — |
| **session_manager** | default | jujutsu, sway, kitty, xvfb-run | — | — | — | — |
| **lifecycle** | podman | jujutsu, golangci-lint | — | bridge, 3847-3848 | — | — |
| **kitchen** | default | WeasyPrint stack (8 pkgs) | grep, find | bridge | skill_dev research | — |
| **yolo_jail** | default | — | — | — | — | — |
| **pi** | podman | — | — | bridge | — | python 3.13 |
| **3d_modeling** | default | jujutsu, openscad, mesa, xvfb-run | — | — | — | — |

### Python Project Patterns

| Project | Has CLI Scripts | Shebang Hack | Dev Run Pattern | Deploy Pattern | mise.toml venv |
|---------|----------------|-------------|-----------------|----------------|----------------|
| **session_manager** | `sm`, `smctl` | ✅ sed in `just setup` | `uv run sm` | `sudo ln -sf` | `{{env.PWD}}/.venv` |
| **pi** | `pictl` | ✅ sed in `just setup` | `uv run pictl` | — | `.venv` |
| **vantage** | `vantage` | ❌ | `uv run vantage` | `uv tool install` | — (.python-version) |
| **copilot-viewer** | `copilot-viewer` | ❌ | `uv run python -m uvicorn` | `uv tool install` | `.venv` |
| **layman** | `layman` | ❌ | `uv run layman` | `uv tool install` | — (.python-version) |
| **kitchen** | — (API only) | ❌ | `uv run uvicorn` | — | — (.python-version) |
| **songtv** | — (API only) | ❌ | `uv run uvicorn` | — | — (.python-version) |
| **sysadmin** | — (scripts) | ❌ | `uv run` | — | `.venv` |
| **hvac_sensors** | — | ❌ | `uv run` | — | — |
| **copilot_cost** | — | ❌ | `uv run` | — | — |

### Non-Python Projects

| Project | Language | Notable Jail Usage |
|---------|----------|--------------------|
| **lifecycle** | Go | Port mapping for web viewer |
| **genius** | Rust | Standard build, no jail workarounds |
| **3d_modeling** | OpenSCAD | Mesa software rendering, xvfb-run for headless |
| **torrent** | Docker/Nix | Docker-compose based |
| **chordpro** | Mixed | Standard |

---

## Problem Analysis

### Problem 1: Shebang Breakage (HIGH IMPACT)

**What happens**: `uv sync` creates scripts in `.venv/bin/` with absolute shebangs:
```
#!/home/matt/code/session_manager/.venv/bin/python3
```
Inside the jail, the workspace is at `/workspace`, so this path doesn't exist.

**Who's affected**: Any Python project with `[project.scripts]` that runs `uv sync` on the host and then enters the jail (or vice versa).

**Current workarounds**:
- `session_manager`: `grep -rl '#!/.*/python' .venv/bin/ | xargs -r sed -i '1s|#!.*/python[0-9.]*|#!/usr/bin/env python3|'`
- `pi`: Same pattern using `rg` instead of `grep`
- `vantage`, `copilot-viewer`, `layman`: Avoid the issue by always using `uv run` for dev

**Why it's painful**: Every new Python project with CLI entry points must either:
1. Copy the shebang-fixing boilerplate into its Justfile, or
2. Avoid ever running CLI scripts directly (always use `uv run`)

### Problem 2: venv Python Symlink Divergence (MEDIUM IMPACT)

**What happens**: `.venv/bin/python` is a symlink to the mise-installed Python:
- Host-created venv: `python → /home/matt/.local/share/mise/installs/python/3.13.7/bin/python3`
- Jail-created venv: `python → /mise/installs/python/3.13.12/bin/python3`

The jail already mounts host mise at its original path (cli.py:1329) so host-created symlinks resolve. But:
- Jail-created venvs point to `/mise/...` which doesn't exist on the host
- `pyvenv.cfg` records `home = /home/matt/.local/share/mise/...` (host-specific)
- Re-running `uv sync` in a different environment recreates the venv with that environment's paths

**Current workaround**: The jail mounts host mise at its original path read-only. This makes host→jail work, but not jail→host.

### Problem 3: The /workspace Path Mismatch (HIGH IMPACT, ROOT CAUSE)

**What happens**: The jail always mounts the workspace at `/workspace` regardless of its host path. This is the root cause of problems 1 and 2.

**What it breaks**:
- Shebangs (absolute paths to .venv/bin/python)
- `pyvenv.cfg` home path
- Session recording (copilot sessions in jail have `cwd: /workspace`, invisible to host DB)
- Any tool that records or compares absolute paths
- Error messages show `/workspace/src/foo.py` instead of the real path

**Why `/workspace` was chosen**: Simplicity and uniformity. Every jail has the same internal path. The entrypoint and AGENTS.md can hardcode `/workspace`.

**Trade-off of changing it**: Mounting at the original host path (e.g., `/home/matt/code/session_manager`) would fix all path issues but:
- Leaks the host username and directory structure into the jail
- Breaks any documentation/instructions that reference `/workspace`
- Different jails would have different workspace paths

### Problem 4: Naked CLI Invocation (MEDIUM IMPACT)

**What happens**: Projects want to type `pictl` or `sm` directly, not `uv run pictl`.

**Inside the jail**: Even if `.venv/bin` were on PATH, the shebangs are broken (Problem 1). And `.venv/bin` is NOT on PATH by default — the jail PATH is:
```
${SHIM_DIR}:/home/agent/.npm-global/bin:/home/agent/go/bin:/mise/shims:/bin:/usr/bin
```

**On the host**: Works if shebangs point to the right Python, or if `mise activate` sets VIRTUAL_ENV.

**Current workarounds**:
- `uv run <tool>` (universal but verbose)
- `session_manager`: `sudo ln -sf` to `/usr/local/bin/sm` (doesn't work in jail — no sudo, read-only /usr/local)
- `vantage`: `uv tool install` puts it on `~/.local/bin/` (separate from .venv)

### Problem 5: Port Mapping Confusion (LOW IMPACT)

**What happens**: With bridge networking, ports are mapped (e.g., 8610→8600). URLs are different inside vs outside the jail.

**Current workaround**: `copilot-viewer` Justfile has comments noting the different URLs. No automated solution.

### Problem 6: Headless Rendering Boilerplate (LOW IMPACT)

**What happens**: `3d_modeling` needs Mesa software rendering and xvfb-run for headless OpenSCAD. This is all in its Justfile with env vars and conditional command wrapping.

**Could the jail help**: The jail could detect GPU-less environments and set `LIBGL_ALWAYS_SOFTWARE=true` automatically. xvfb-run is already a nix package option.

### Problem 7: Blocked Tools Not Standardized (LOW IMPACT)

**What happens**: Only `vantage` and `kitchen` block grep→rg and find→fd. Other projects don't, leading to inconsistent agent behavior.

**Could be fixed**: Make blocked tools a user-level default in `~/.config/yolo-jail/config.jsonc` so all projects get the same baseline.

---

## Roadmap

### Phase 1: Automatic Shebang Fixing (Quick Win)

**Goal**: Eliminate the shebang-fixing boilerplate from every project's Justfile.

**Approach**: Wrap `uv sync` inside the jail so that after it completes, shebangs in `.venv/bin/` are automatically normalized to `#!/usr/bin/env python3`.

**Implementation options** (pick one):

#### Option A: Post-sync hook via uv wrapper shim
Create a `uv` wrapper in the jail that intercepts `uv sync` and `uv pip install` commands:
```bash
#!/bin/bash
# ~/.local/bin/uv-wrapper
/mise/shims/uv "$@"
status=$?
if [[ "$1" == "sync" || "$1" == "pip" ]] && [[ $status -eq 0 ]]; then
    # Fix shebangs in .venv/bin/ if it exists
    if [ -d ".venv/bin" ]; then
        grep -rl '#!/.*/python' .venv/bin/ 2>/dev/null | \
            xargs -r sed -i '1s|#!.*/python[0-9.]*|#!/usr/bin/env python3|'
    fi
fi
exit $status
```

**Pros**: Transparent, works for all projects, no project-side changes needed.
**Cons**: Wrapper approach is fragile (uv subcommand detection), could mask errors.

#### Option B: PROMPT_COMMAND-based watcher
Check for broken shebangs in `.venv/bin/` on every prompt and fix them silently. Too noisy and wasteful.

#### Option C: `yolo fix-venv` command (recommended first step)
Add a `yolo fix-venv` subcommand that fixes shebangs and can be called from Justfiles:
```just
setup:
    uv sync
    yolo fix-venv  # no-op outside jail, fixes shebangs inside
```

The `yolo` CLI is available inside the jail (mounted from `/opt/yolo-jail`). Making it a proper subcommand means:
- It's discoverable (`yolo --help`)
- It's a no-op outside the jail (or when shebangs are already correct)
- Projects can call it explicitly in `just setup`
- It can also fix pyvenv.cfg and symlinks

**Recommended**: Start with Option C, consider Option A later as an automatic enhancement.

#### Option D: Teach `uv` itself
`uv` doesn't have a `--portable-shebangs` flag. This could be an upstream contribution but is a longer-term effort.

### Phase 2: .venv/bin on PATH (Depends on Phase 1)

**Goal**: Let agents type `pictl` instead of `uv run pictl` inside the jail.

**Approach**: After shebangs are fixed (Phase 1), add `.venv/bin` to PATH in the jail's bashrc:
```bash
# In generate_bashrc()
if [ -d "/workspace/.venv/bin" ]; then
    export PATH="/workspace/.venv/bin:$PATH"
    export VIRTUAL_ENV="/workspace/.venv"
fi
```

**Consideration**: This should come AFTER the shim dir in PATH so blocked tools still work. And it should be opt-in or at least not break projects without a venv.

**Alternative**: Use `mise activate` which already handles VIRTUAL_ENV and PATH when `_.python.venv` is configured in `mise.toml`. The jail already runs `eval "$(mise activate bash)"` in bashrc — this might already work if the venv exists. **Investigate whether mise activate already adds .venv/bin to PATH.**

### Phase 3: Workspace Path Transparency (Bigger Change)

**Goal**: Eliminate the `/workspace` vs host path divergence entirely.

**Approach**: Mount the workspace at its original host path inside the jail. Create `/workspace` as a compatibility symlink.

```python
# In cli.py run()
workspace_path = str(workspace)  # e.g., "/home/matt/code/session_manager"
docker_cmd.extend(["-v", f"{workspace}:{workspace_path}"])
docker_cmd.extend(["-w", workspace_path])
# Pass both for backward compat
docker_cmd.extend(["-e", f"YOLO_WORKSPACE={workspace_path}"])
```

In the entrypoint:
```python
# Create /workspace symlink for backward compat
os.symlink(os.environ["YOLO_WORKSPACE"], "/workspace")
```

**Benefits**:
- Shebangs created on host work inside jail without fixing
- pyvenv.cfg paths are correct
- Session cwds match between host and jail
- Error messages show real paths
- `git` operations record real paths

**Risks**:
- Leaks host username/path structure (acceptable for a personal tool)
- Any hardcoded `/workspace` references in AGENTS.md, skills, or entrypoint need updating
- Nested jails need to pass through the original path

**Migration**:
1. Add `YOLO_WORKSPACE` env var with the real path
2. Mount at real path, create `/workspace` symlink
3. Update AGENTS.md generation to use `$YOLO_WORKSPACE`
4. Update entrypoint to use `$YOLO_WORKSPACE` as cwd
5. Keep `/workspace` symlink for one release cycle, then remove

### Phase 4: User-Level Defaults (Quick Win, Independent)

**Goal**: Standardize blocked tools and other defaults across all projects.

**Approach**: The user-level config (`~/.config/yolo-jail/config.jsonc`) already supports this. Create a recommended default:

```jsonc
{
    // Block grep/find in favor of rg/fd across all projects
    "blocked_tools": {
        "grep": { "suggestion": "rg (ripgrep)" },
        "find": { "suggestion": "fd" }
    },
    // jj is used in most projects
    "packages": ["jujutsu"],
    // Default runtime
    "runtime": "podman"
}
```

Projects can override in their workspace `yolo-jail.jsonc`. This eliminates the need for every project to independently block grep/find.

**Note**: `blocked_tools` at the user level doesn't exist yet — currently only workspace-level. Adding user-level defaults with merge semantics is needed.

### Phase 5: `yolo doctor` Enhancements (Medium-term)

**Goal**: Detect and report venv/path issues automatically.

**Add checks to `yolo doctor`**:
- `.venv/bin/` scripts with non-portable shebangs
- `.venv/bin/python` symlinks pointing to nonexistent paths
- `pyvenv.cfg` with paths that don't exist in the current environment
- mise.toml missing `_.python.venv` config
- Projects using `.python-version` without `mise.toml` (suggest migration)

### Phase 6: Project Scaffolding Updates (Medium-term)

**Goal**: New projects work perfectly in the jail from day one.

**Update the `new-project` skill**:
1. Add `yolo fix-venv` to the standard `just setup` recipe
2. Recommend `mise.toml` with `_.python.venv` over bare `.python-version`
3. Add jail-specific notes to the generated AGENTS.md
4. Include `yolo-jail.jsonc` in the scaffolding (even if empty `{}`)

**Standard Justfile recipe**:
```just
setup:
    uv sync
    -yolo fix-venv 2>/dev/null  # Fix shebangs for jail compatibility (no-op outside jail)
```

### Phase 7: Session Path Reconciliation (Long-term)

**Goal**: Copilot sessions created in the jail are visible to the host session DB.

**Problem**: Jail sessions have `cwd: /workspace` which doesn't match `cwd: /home/matt/code/project` in the host DB. The `session_manager` has custom logic to check both paths.

**Approach**: With Phase 3 (workspace path transparency), this is solved automatically — sessions will record the real host path as cwd.

**If Phase 3 is deferred**: The jail could set `YOLO_HOST_DIR` and tools that record cwds could be taught to use it. But this requires patching copilot/gemini behavior, which isn't feasible.

---

## Priority Matrix

| Phase | Effort | Impact | Dependencies | Recommendation |
|-------|--------|--------|--------------|----------------|
| **Phase 1: fix-venv** | Small | High | None | **Do first** |
| **Phase 2: .venv on PATH** | Small | Medium | Phase 1 | Do second |
| **Phase 3: Path transparency** | Large | High | None | **Design now, implement after Phase 1** |
| **Phase 4: User defaults** | Small | Medium | None | Do anytime |
| **Phase 5: Doctor checks** | Small | Low | None | Do anytime |
| **Phase 6: Scaffolding** | Small | Medium | Phase 1 | After Phase 1 |
| **Phase 7: Session paths** | N/A | Medium | Phase 3 | Solved by Phase 3 |

---

## Appendix: Project-Specific Observations

### session_manager
- Has the most sophisticated jail awareness — its AGENTS.md documents the dual session store (jail vs host) and explains `cwd: /workspace` divergence
- Uses `{{env.PWD}}/.venv` in mise.toml (dynamic absolute path) — likely an attempt to make venv paths work across environments
- Installs to `/usr/local/bin/sm` via sudo — impossible in jail

### copilot-viewer
- Acts as a central aggregator — mounts 10+ project `.yolo` directories read-only
- Port mapping (8610→8600) requires documenting both URLs
- Could benefit from a discovery mechanism instead of manual mount enumeration

### kitchen
- Most complex package requirements (WeasyPrint stack with pinned freetype)
- Proves the nix package system works well for complex native deps
- No venv issues because it's an API-only project (no CLI scripts)

### 3d_modeling
- Creative use of conditional commands in Justfile for headless rendering
- `LIBGL_ALWAYS_SOFTWARE` and xvfb-run wrapping is boilerplate that could be a jail feature
- Proves the jail works for non-Python workloads

### vantage/copilot-viewer/layman
- These three projects represent the "golden path" — they use `uv tool install` for deployment and `uv run` for dev, completely avoiding venv path issues
- Could be the recommended pattern going forward, with `yolo fix-venv` as a safety net for projects that can't follow this pattern

### pi
- Uses `mise_tools` to specify Python version — the only project doing this
- Has `host` network mode for HA API access — proves network config flexibility
- Shebang fix is identical to session_manager but uses `rg` instead of `grep`

---

## Open Questions

1. **Should Phase 3 (path transparency) replace `/workspace` entirely, or should it be opt-in?** Backward compatibility vs cleaner semantics. Recommendation: replace with symlink for compat.

2. **Should `yolo fix-venv` be automatic (Phase 1 Option A) or explicit (Phase 1 Option C)?** Explicit is safer and more discoverable, but automatic eliminates the need for project-side changes.

3. **Should blocked tools be inherited from user config?** Currently only workspace-level. User-level defaults would reduce per-project config duplication but need clear override semantics (workspace replaces user? workspace extends user?).

4. **Should mise activation automatically add .venv/bin to PATH?** It may already do this — needs testing. If so, Phase 2 is free.

5. **Is the `uv tool install` pattern (vantage/copilot-viewer/layman) preferable to fixing shebangs?** If so, the new-project skill should recommend it as the primary pattern, with `fix-venv` as a fallback.
