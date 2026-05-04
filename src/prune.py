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
from typing import Callable, Iterable, List, Tuple

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

# Subdirs under ``~/.cache`` that are safe to purge by age — content
# is pure CAS with a fast re-download path.  ``go-build`` is
# re-compile-able.  Heavy re-downloads (playwright browsers, HF
# models) live in a separate opt-in set below.
#
# ``pex`` and ``pants`` caches rebuild on next invocation (wheels +
# cached interpreters); the rebuild can take a few minutes but content
# is fully reproducible from pypi + python-build-standalone.
# ``node-gyp`` and ``gopls`` are small tool caches, trivially
# recomputed.  NOT listed here (intentionally): ``copilot`` (holds the
# actual CLI installation at pkg/, not re-downloadable state) and
# anything under the forbidden browser set.
CACHE_PURGE_DEFAULT_SUBDIRS: Tuple[str, ...] = (
    "uv",
    "pip",
    "npm",
    "go-build",
    "mise",
    "pex",
    "pants",
    "node-gyp",
    "gopls",
)

# Opt-in.  Same safety profile but the re-fetch cost is meaningful:
# playwright re-downloads are ~400 MiB per browser; huggingface
# models can be GiBs each.
CACHE_PURGE_HEAVY_SUBDIRS: Tuple[str, ...] = ("ms-playwright", "huggingface")

# Hard-excluded.  These aren't pure caches — they carry live user
# profile state (cookies, IndexedDB, extensions, bookmarks), OR they
# hold the installed binaries of a tool (copilot's pkg/) rather than
# regenerable cache.  ``_purge_cache_by_age`` refuses to touch them
# even if the caller explicitly names them in ``subdirs``.
_CACHE_PURGE_FORBIDDEN = frozenset(
    {
        "chromium",
        "google-chrome",
        "chrome",
        "mozilla",
        "firefox",
        "thunderbird",
        "copilot",
    }
)

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
    entries: List[DedupEntry],
    *,
    apply: bool,
    progress_cb: "Callable[..., None] | None" = None,
) -> Tuple[int, int]:
    """Group ``entries`` by (size, sha256) and hardlink duplicates.

    Within each duplicate group the first entry becomes the canonical
    inode and the rest get ``os.link``'d to it.  Files that already
    share an inode are skipped (no wasted work, no double-count).

    Returns ``(bytes_saved, links_made)``.  Dry-run (``apply=False``)
    returns the same numbers it WOULD have produced but performs no
    filesystem mutations.

    ``progress_cb``, if passed, is invoked with ``advance=1`` once per
    duplicate-decision that results in a link (real or dry-run).  It
    is NOT called for solo / already-linked entries — advancing the
    bar in those cases would badly over-count, given that typical
    dedup runs have ~1M input entries but only a small fraction are
    actual duplicates.
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
                    if progress_cb is not None:
                        progress_cb(advance=1)
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
                if progress_cb is not None:
                    progress_cb(advance=1)
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
      - ``cache_breakdown``: {subdir_name: bytes} for every direct
        child of ``global_storage/cache``.  The cache bucket typically
        dominates; surfacing its internal breakdown tells the operator
        which tool cache is actually fat (images? pip? go-build?).
        Empty dict if ``cache/`` doesn't exist.
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

    cache_breakdown: dict[str, int] = {}
    cache_root = global_storage / "cache"
    if cache_root.is_dir():
        stray = 0
        try:
            entries = list(cache_root.iterdir())
        except OSError:
            entries = []
        for child in entries:
            try:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    cache_breakdown[child.name] = _dir_size_bytes(child)
                elif child.is_file():
                    stray += child.lstat().st_size
            except OSError:
                continue
        if stray:
            cache_breakdown["_files"] = stray

    ws_bytes = sum(_dir_size_bytes(ws / ".yolo") for ws in workspaces)
    return {
        "global_storage": gs_bytes,
        "workspaces": ws_bytes,
        "total": gs_bytes + ws_bytes,
        "breakdown": breakdown,
        "cache_breakdown": cache_breakdown,
    }


# ---------------------------------------------------------------------------
# Age-based cache purge
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shadowed-home prune — purge :ro seed subtrees that are overlay-masked
# at jail runtime so they can never be read.  Pre-cache-split cruft
# typically hoards 70+ GiB under `.cache`, and also accumulates under
# `.npm`/`.npm-global`/`.local`/`go` for operators who ran the jail
# before those were split out into per-workspace overlays.
#
# SAFETY: every path here MUST be fully shadowed by an overlay bind
# mount declared in cli.py's docker_cmd (or by a redirect env var like
# NPM_CONFIG_CACHE that points reads elsewhere while the :ro base makes
# writes fail).  The drift-check in tests/test_prune.py greps cli.py
# at test time to enforce this — if an overlay mount is removed without
# trimming this list, tests fail and CI blocks the change.
#
# NOT listed (intentionally):
#   - .copilot / .gemini / .claude — seeded by _seed_agent_dir on every
#     new workspace; deleting the base would break first-boot auth.
#   - .claude-shared-credentials — rw shared-credential mount point.
#   - .config — bind-mounted over but the seed may carry non-auth
#     content the entrypoint relies on; too risky to add without audit.
# ---------------------------------------------------------------------------

SHADOWED_HOME_PATHS: Tuple[str, ...] = (
    ".cache",
    ".npm",
    ".npm-global",
    ".local",
    "go",
)


def _prune_shadowed_home(
    global_home: Path,
    *,
    apply: bool,
) -> Tuple[int, int]:
    """Delete subpaths of ``global_home`` listed in
    ``SHADOWED_HOME_PATHS``.  Returns ``(bytes_removed, items_removed)``.

    Symlinks are unlinked but never traversed — if the operator
    relocated a shadowed dir to cold storage via ``ln -s``, the real
    content on the HDD target is preserved.  The symlink itself has
    no on-disk cost and can be recreated by the next jail boot (the
    overlay mount will materialize the path fresh).

    Entries containing ``..`` are refused defensively even though the
    registry is a compile-time constant.
    """
    if not global_home.is_dir():
        return 0, 0

    bytes_removed = 0
    items_removed = 0

    import shutil as _shutil

    for rel in SHADOWED_HOME_PATHS:
        if ".." in rel.split("/") or rel.startswith("/"):
            log.debug("refusing suspicious registry entry %r", rel)
            continue
        target = global_home / rel
        try:
            lst = target.lstat()
        except OSError:
            continue

        import stat as _stat

        if _stat.S_ISLNK(lst.st_mode):
            # Symlink itself takes ~0 bytes but still counts as one item.
            if apply:
                try:
                    target.unlink()
                except OSError as e:
                    log.debug("unlink symlink %s failed: %s", target, e)
                    continue
            items_removed += 1
            continue

        if _stat.S_ISDIR(lst.st_mode):
            size = _dir_size_bytes(target)
            if apply:
                try:
                    _shutil.rmtree(target)
                except OSError as e:
                    log.debug("rmtree %s failed: %s", target, e)
                    continue
            bytes_removed += size
            items_removed += 1
            continue

        if _stat.S_ISREG(lst.st_mode):
            size = lst.st_size
            if apply:
                try:
                    target.unlink()
                except OSError as e:
                    log.debug("unlink %s failed: %s", target, e)
                    continue
            bytes_removed += size
            items_removed += 1

    return bytes_removed, items_removed


# ---------------------------------------------------------------------------
# Image-cache prune (keep newest N, sweep orphan tmp files)
# ---------------------------------------------------------------------------


def _prune_image_cache(
    images_dir: Path,
    *,
    keep: int,
    apply: bool,
) -> Tuple[int, int]:
    """Keep the ``keep`` newest ``*.tar`` files under ``images_dir`` and
    remove the rest.  Always sweeps orphan ``*.tmp`` files (leftovers
    from a crashed ``_materialize_image``) regardless of ``keep``.

    Returns ``(bytes_removed, files_removed)``.  Dry-run reports what
    would go without touching disk.

    Why: the image tar cache can hit 80+ GiB (one ~3 GiB tar per
    distinct nix store path, and every config change / package bump
    mints a new one).  The fallback path in ``auto_load_image`` only
    needs the newest one or two as a recovery option when ``nix build``
    fails inside a nested jail — anything older is pure cruft.
    """
    if not images_dir.is_dir():
        return 0, 0

    bytes_removed = 0
    files_removed = 0

    try:
        children = list(images_dir.iterdir())
    except OSError:
        return 0, 0

    tars: List[Tuple[Path, int, float]] = []
    tmps: List[Tuple[Path, int]] = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_file():
                continue
            st = child.lstat()
        except OSError:
            continue
        if child.suffix == ".tar":
            tars.append((child, st.st_size, st.st_mtime))
        elif child.suffix == ".tmp":
            tmps.append((child, st.st_size))

    # Tars: newest first, drop the tail beyond `keep`.
    tars.sort(key=lambda t: t[2], reverse=True)
    for path, size, _ in tars[keep:]:
        if apply:
            try:
                path.unlink()
            except OSError as e:
                log.debug("unlink %s failed: %s", path, e)
                continue
        bytes_removed += size
        files_removed += 1

    # Orphan tmp files: always sweep.  A live materialization holds an
    # open fd on its .tmp, but unlink() is safe — the fd keeps the
    # inode alive until close.  Still, the risk is low enough and the
    # win high enough that we just do it.
    for path, size in tmps:
        if apply:
            try:
                path.unlink()
            except OSError as e:
                log.debug("unlink %s failed: %s", path, e)
                continue
        bytes_removed += size
        files_removed += 1

    return bytes_removed, files_removed


def _purge_cache_by_age(
    cache_root: Path,
    *,
    subdirs: Iterable[str],
    older_than_days: float,
    apply: bool,
) -> Tuple[int, int]:
    """Remove regular files older than ``older_than_days`` under each
    named ``subdir`` of ``cache_root``.  Returns
    ``(bytes_removed, files_removed)``.

    Safety:
      - Only subdirs the caller explicitly names are scanned — no
        glob, no recursive allowlist expansion.
      - Browser profile dirs (chromium family, firefox) are HARD-
        EXCLUDED even if the caller names them, because they hold
        live user state (cookies, IndexedDB, extensions) that looks
        like cache-shaped files but is authoritative session data.
      - Symlinks are never followed or deleted (target might live
        outside the cache subtree).
      - Dry-run (``apply=False``) returns accurate counts but makes
        no filesystem changes.
    """
    import time

    cutoff = time.time() - (older_than_days * 86400)

    bytes_removed = 0
    files_removed = 0

    for sub in subdirs:
        if sub in _CACHE_PURGE_FORBIDDEN:
            log.debug("refusing to purge browser-profile subdir %s", sub)
            continue
        root = cache_root / sub
        if not root.is_dir():
            continue
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
                # Use mtime — atime is often disabled (noatime) or
                # relatime-delayed, which would under-report
                # staleness.  mtime of cache files is the download
                # time and doesn't change after write.
                if st.st_mtime >= cutoff:
                    continue
                size = st.st_size
                if apply:
                    try:
                        p.unlink()
                    except OSError as e:
                        log.debug("unlink %s failed: %s", p, e)
                        continue
                bytes_removed += size
                files_removed += 1
    return bytes_removed, files_removed
