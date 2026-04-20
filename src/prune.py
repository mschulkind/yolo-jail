"""Pure-function backing logic for ``yolo prune``.

Every function here is side-effect-free unless it's explicitly invoked
with ``apply=True``.  The CLI wrapper in ``src/cli.py`` defaults to
dry-run so a user who just types ``yolo prune`` sees a report and
nothing more.

Why pure functions in their own module: the CLI code is already large
and intimately entangled with typer + subprocess.  Isolating the
reclaim primitives here keeps them easy to unit-test (see
``tests/test_prune.py``) and leaves the door open for a scheduled-
invocation path later (cron / systemd timer) that calls the same
primitives without going through typer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

log = logging.getLogger("yolo.prune")

# Dedup is scoped to three subtrees per workspace.  They're the only
# ones where ``npm install -g``, ``pip install``, and ``go install``
# produce content that's bit-identical across projects.  Agent config
# dirs (``.claude/`` etc.) are explicitly excluded — those carry
# per-workspace state that must never be shared.
_DEDUPE_SUBTREES = ("npm-global", "local", "go")

# Under GLOBAL_STORAGE these subtrees hold content-addressable-ish
# blobs (downloaded wheels, tarballs, prebuilt binaries) that are
# safe to hardlink across and within.  ``containers`` and ``agents``
# hold per-host bookkeeping + per-jail state that MUST NOT be shared.
# ``state`` holds loophole runtime state (flock files, CA keys) —
# also not safe.  ``nix-build-root`` and ``build`` are too small to
# matter and change on every rebuild.
_GLOBAL_DEDUPE_SUBDIRS = ("cache", "mise", "home")

# Hardlink detection reads files in chunks; avoid loading multi-GB
# binaries into memory all at once.
_HASH_CHUNK_BYTES = 1 << 20  # 1 MiB


# ---------------------------------------------------------------------------
# Workspace discovery via container-runtime metadata
# ---------------------------------------------------------------------------


def _find_yolo_workspaces(runtime: str) -> List[Path]:
    """Return deduplicated, resolved workspace paths for every yolo-*
    container the runtime knows about (running or stopped).

    Discovery via ``ps`` + ``inspect`` (rather than a persistent
    registry on disk) means a single source of truth — whatever the
    runtime says — and no stale-file cleanup of our own.
    """
    try:
        res = subprocess.run(
            [runtime, "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []

    names = [
        line.strip()
        for line in res.stdout.splitlines()
        if line.strip().startswith("yolo-")
    ]

    found: List[Path] = []
    seen: set[Path] = set()
    for name in names:
        ws = _inspect_workspace_mount(runtime, name)
        if ws is None:
            continue
        resolved = ws.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def _inspect_workspace_mount(runtime: str, name: str) -> "Path | None":
    """Return the host path bound into ``/workspace`` for ``name``, or
    None if the container has no such mount / inspect fails."""
    try:
        res = subprocess.run(
            [runtime, "inspect", "--format", "{{json .Mounts}}", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        mounts = json.loads(res.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(mounts, list):
        return None
    for m in mounts:
        if isinstance(m, dict) and m.get("Destination") == "/workspace":
            src = m.get("Source")
            if isinstance(src, str) and src:
                return Path(src)
    return None


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------


@dataclass
class DedupEntry:
    """A candidate file for the hardlink dedup pass.

    ``size`` is stat()ed up front (cheap, sequential walk).  Hashes
    are computed lazily in the dedup pass — only for files whose
    ``size`` collides with another file's size, i.e. the files that
    might actually be duplicates.  Avoids reading gigabytes of unique
    node_modules bytes just to confirm they're unique.
    """

    path: Path
    size: int


def _walk_dedupable_files(workspaces: Iterable[Path]) -> Iterable[DedupEntry]:
    """Yield a ``DedupEntry`` for every regular, non-empty file under
    each workspace's ``.yolo/home/{npm-global,local,go}`` tree.
    Symlinks are skipped (linking onto a link target is a footgun)
    and zero-byte files are skipped (can't save bytes)."""
    for ws in workspaces:
        home = ws / ".yolo" / "home"
        for sub in _DEDUPE_SUBTREES:
            yield from _walk_dedup_tree(home / sub)


def _walk_global_dedupable(global_storage: Path) -> Iterable[DedupEntry]:
    """Yield a ``DedupEntry`` for every regular, non-empty file under
    the shared global-storage subtrees that are safe to hardlink-dedup:
    ``cache/`` (pip/uv/npm/playwright/mise/… downloaded artifacts),
    ``mise/`` (installed tool versions — often share libraries across
    patch-level installs), and ``home/`` (the :ro base seed).
    Never touches ``containers/``, ``agents/``, ``state/``, or the
    nix/build scratch dirs — those hold per-host bookkeeping or
    ephemeral build output."""
    for sub in _GLOBAL_DEDUPE_SUBDIRS:
        yield from _walk_dedup_tree(global_storage / sub)


def _walk_dedup_tree(root: Path) -> Iterable[DedupEntry]:
    """Shared walker used by both dedup scopes.  Yields regular,
    non-empty, non-symlink files under ``root`` as ``DedupEntry``s.
    Silently returns nothing if ``root`` doesn't exist."""
    if not root.is_dir():
        return
    import stat as _stat

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for name in filenames:
            p = dp / name
            try:
                st = p.lstat()
            except OSError:
                continue
            if _stat.S_ISLNK(st.st_mode):
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            if st.st_size == 0:
                continue
            yield DedupEntry(path=p, size=st.st_size)


# ---------------------------------------------------------------------------
# Hardlink dedup
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> "str | None":
    """SHA-256 the file at ``path`` in streaming chunks.  Returns
    None on I/O error so the caller can skip the file."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _hardlink_duplicate_files(
    entries: List[DedupEntry], *, apply: bool
) -> Tuple[int, int]:
    """Group ``entries`` by (size, sha256) and hardlink duplicates.

    Within each duplicate group the first entry becomes the canonical
    inode and the rest get ``os.link``'d to it.  Files that already
    share an inode are skipped (no wasted work, no double-count).

    Returns ``(bytes_saved, links_made)``.  Dry-run (``apply=False``)
    returns the same numbers it WOULD have produced but performs no
    filesystem mutations.
    """
    # Group by size first (cheap filter) — only hash files whose size
    # collides with at least one other file.  Saves reading gigabytes
    # of unique-sized installed binaries.
    by_size: dict[int, List[DedupEntry]] = {}
    for e in entries:
        by_size.setdefault(e.size, []).append(e)

    bytes_saved = 0
    links_made = 0

    for size, group in by_size.items():
        if len(group) < 2:
            continue
        # Hash each file in the group, bucket by hash.
        by_hash: dict[str, List[DedupEntry]] = {}
        for e in group:
            digest = _hash_file(e.path)
            if digest is None:
                continue
            by_hash.setdefault(digest, []).append(e)

        for same in by_hash.values():
            if len(same) < 2:
                continue
            canonical = same[0]
            try:
                canonical_ino = canonical.path.stat().st_ino
                canonical_dev = canonical.path.stat().st_dev
            except OSError:
                continue
            for dup in same[1:]:
                try:
                    dup_st = dup.path.stat()
                except OSError:
                    continue
                # Already the same inode (previous dedup run, or
                # the installer itself made the link) — skip.
                if dup_st.st_ino == canonical_ino and dup_st.st_dev == canonical_dev:
                    continue
                if not apply:
                    bytes_saved += size
                    links_made += 1
                    continue
                # Atomic link-over-replace: link to a temp name
                # then rename over the original.  A partial failure
                # leaves the original file intact (never deletes
                # before the link is confirmed).
                tmp = dup.path.with_name(dup.path.name + ".yolo-dedup-tmp")
                try:
                    os.link(canonical.path, tmp)
                except OSError as exc:
                    log.debug("link %s → %s failed: %s", canonical.path, tmp, exc)
                    # Clean up if tmp partially exists.
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    os.replace(tmp, dup.path)
                except OSError as exc:
                    log.debug("replace %s ← %s failed: %s", dup.path, tmp, exc)
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                    continue
                bytes_saved += size
                links_made += 1
    return bytes_saved, links_made


# ---------------------------------------------------------------------------
# Stopped-container prune (yolo-* only)
# ---------------------------------------------------------------------------


def _prune_stopped_containers(runtime: str, *, apply: bool) -> List[str]:
    """Remove stopped containers whose name starts with ``yolo-``.

    Returns the list of names removed (or that WOULD be removed in
    dry-run).  Only touches containers whose name starts with
    ``yolo-`` — never any other container on the user's host.
    """
    try:
        res = subprocess.run(
            [runtime, "ps", "-a", "--format", "{{.Names}} {{.State}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []

    targets: List[str] = []
    for line in res.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        if not name.startswith("yolo-"):
            continue
        # "running" is case-insensitive — podman emits "Running"
        # or "running"; filter anything that's clearly still alive.
        if state.lower() in ("running", "paused", "restarting"):
            continue
        targets.append(name)

    if apply:
        for name in targets:
            try:
                subprocess.run(
                    [runtime, "rm", name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                continue
    return targets


# ---------------------------------------------------------------------------
# Old-image prune (keep latest N)
# ---------------------------------------------------------------------------


def _prune_old_images(runtime: str, *, keep: int, apply: bool) -> List[str]:
    """List yolo-jail images sorted newest-first; rm all but the
    newest ``keep``.  Returns the image IDs removed (or slated for
    removal in dry-run)."""
    try:
        res = subprocess.run(
            [
                runtime,
                "images",
                "--format",
                "{{.ID}} {{.Repository}}:{{.Tag}} {{.CreatedAt}}",
                "yolo-jail",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []

    # Each line: "<id> <repo>:<tag> <ISO-ish created>"
    images: List[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        img_id, _repo_tag, created = parts
        images.append((img_id, created))

    # Sort newest-first by the created timestamp; ISO 8601 sorts
    # lexically so no need to parse.
    images.sort(key=lambda t: t[1], reverse=True)

    to_remove = [img_id for img_id, _ in images[keep:]]

    if apply:
        for img_id in to_remove:
            try:
                subprocess.run(
                    [runtime, "rmi", "-f", img_id],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                continue
    return to_remove


# ---------------------------------------------------------------------------
# Disk-usage report
# ---------------------------------------------------------------------------


def _dir_size_bytes(p: Path) -> int:
    """Sum sizes of regular files under ``p``.  Missing path → 0."""
    if not p.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(p, followlinks=False):
        dp = Path(dirpath)
        for name in filenames:
            try:
                total += (dp / name).lstat().st_size
            except OSError:
                continue
    return total


def _disk_usage_report(*, workspaces: Iterable[Path], global_storage: Path) -> dict:
    """Per-category byte totals for everything a prune might reclaim.

    Keys:
      - ``global_storage``: total size under ``~/.local/share/yolo-jail``
      - ``workspaces``: sum of ``.yolo/`` sizes across known workspaces
      - ``total``: sum of the above
      - ``breakdown``: {subdir_name: bytes} for every direct child of
        ``global_storage`` so the operator can see WHERE the bytes are
        (cache? mise? containers?).  Stray top-level files roll up
        into ``"_files"`` so the breakdown sum equals the top-level
        total exactly — nothing stays hidden.
    """
    breakdown: dict[str, int] = {}
    gs_bytes = 0
    if global_storage.is_dir():
        stray = 0
        try:
            entries = list(global_storage.iterdir())
        except OSError:
            entries = []
        for child in entries:
            try:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    size = _dir_size_bytes(child)
                    breakdown[child.name] = size
                    gs_bytes += size
                elif child.is_file():
                    size = child.lstat().st_size
                    stray += size
                    gs_bytes += size
            except OSError:
                continue
        if stray:
            breakdown["_files"] = stray

    ws_bytes = sum(_dir_size_bytes(ws / ".yolo") for ws in workspaces)
    return {
        "global_storage": gs_bytes,
        "workspaces": ws_bytes,
        "total": gs_bytes + ws_bytes,
        "breakdown": breakdown,
    }
