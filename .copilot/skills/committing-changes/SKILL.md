---
name: committing-changes
description: How to commit changes in this repo using jj. Covers the public/private split, rev descriptions, and the staging workflow.
---

# Committing Changes

This repo uses **jj** (Jujutsu) for version control, colocated with git. There is a public/private split: the tool is open-sourced at `github.com/mschulkind/<PUBLIC_NAME>`, while private infrastructure, internal docs, and agent output live only in the private repo.

**You must follow this workflow for all changes.**

---

## Core Concepts

### Bookmarks

- **`main`**: The public head. Points to the latest commit pushed to both repos. Only public-appropriate content exists at this revision.
- **`staging`**: Empty rev on top of `main`. Accumulates public-worthy changes before they're squashed into `main`.
- **`dev`**: The private working head. Descends from `staging`. Contains everything including private files.

### Rev Structure

At any point the graph looks like:

```
@  (your working copy — always a descendant of dev)
│
○  dev (private, all files, pushed to private remote only)
│
○  staging (accumulating public changes for next release)
│
◆  main (public head, pushed to both remotes as 'main')
```

---

## When Making Changes

### 1. Determine Public vs Private

**Public** (goes into `staging`):
- Source code: `src/cli.py`, `src/entrypoint.py`, `src/shims/`, `src/__init__.py`
- Tests: `tests/`
- Build config: `pyproject.toml`, `Justfile`, `flake.nix`, `flake.lock`, `mise.toml`, `uv.lock`
- Docs: `README.md`, `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`
- Public docs: `docs/config-safety.md`, `docs/storage-and-config.md`
- CI/CD: `.github/`
- Package manifest: `.gitignore`, `.python-version`

**Private** (stays on `dev`, never squashed into `staging`):
- `AGENTS.md` — agent instructions
- `.copilot/`, `.yolo/` — agent skills, jail state
- `yolo-jail.jsonc` — personal jail config
- `yolo-enter.sh` — personal entry wrapper
- `OPEN_QUESTIONS.md` — internal decisions
- `docs/NAME_CANDIDATES.md`, `docs/OPEN_SOURCE_CHECKLIST.md` — internal docs
- `docs/plans/`, `docs/research/`, `docs/tasks/` — internal planning
- `docs/kitty-jail.conf` — personal config
- `scratch/` — scratch files
- `result` — nix build output symlink

**When in doubt:** If the change is about the tool itself (code, user-facing docs, build config), it's public. If it's about infrastructure, internal process, or agent workflow, it's private.

### 2. Squash Public Changes into Staging

```bash
jj squash --into staging path/to/file1 path/to/file2
```

### 3. Squash Private Changes into `dev`

```bash
jj squash --into dev
```

### 4. Always Update Rev Descriptions

**Every time you squash changes into a rev, update its description** to accurately reflect all the changes it now contains.

```bash
jj describe -r staging -m "feat: add doctor command for environment health checks

- Add yolo doctor subcommand
- Check container runtime, nix, image status
- Validate config files"
```

Good descriptions:
- Summarize ALL changes in the rev (not just the latest squash)
- Use conventional commit style when possible
- **`staging` must always have a release-ready description** — it becomes the public commit message

---

## Releasing to Public

```bash
# 1. Ensure staging description is ready
jj describe -r staging -m "your release description"

# 2. Promote (runs quality gates, moves staging→main, pushes)
just promote
```

---

## Starting a New Task

```bash
jj log -r 'main | staging | dev | @'
jj new dev
# ... make changes ...
# squash into staging (public) or dev (private)
```

---

## Summary

| Change type | Squash into | Push to |
|-------------|-------------|---------|
| Tool code, public docs, config | `staging` (then `main` on release) | `main` → both remotes |
| Internal docs, agent output, infra | `dev` | `dev` → private only |
