"""Tests for src.prune — `yolo prune` backing logic.

Pure functions live in src/prune.py; the CLI wiring lives in src/cli.py.
Every function here is written test-first: dry-run by default, no
filesystem side effects unless a test explicitly sets up a fake tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock


from src import prune


# ---------------------------------------------------------------------------
# Workspace discovery via container-runtime metadata
# ---------------------------------------------------------------------------


class TestFindYoloWorkspaces:
    """`_find_yolo_workspaces(runtime)` enumerates every live / stopped
    container whose name starts with ``yolo-`` and reports the host
    path bound into ``/workspace``.  That's the only reliable source
    of "which projects does this user run yolo-jail against" since
    per-project state lives under ``<project>/.yolo/`` (not a central
    registry)."""

    def _fake_run(self, mapping: dict[tuple[str, ...], str]):
        """Return a subprocess.run stub keyed by argv tuple."""

        def runner(cmd, **kwargs):
            out = mapping.get(tuple(cmd), "")
            return MagicMock(returncode=0, stdout=out, stderr="")

        return runner

    def test_returns_empty_list_when_no_yolo_containers(self, monkeypatch):
        monkeypatch.setattr(
            prune.subprocess,
            "run",
            self._fake_run({("podman", "ps", "-a", "--format", "{{.Names}}"): ""}),
        )
        assert prune._find_yolo_workspaces("podman") == []

    def test_returns_workspace_for_each_yolo_container(self, monkeypatch, tmp_path):
        ws_a = tmp_path / "project-a"
        ws_a.mkdir()
        ws_b = tmp_path / "project-b"
        ws_b.mkdir()
        inspect_a = json.dumps(
            [
                {
                    "Destination": "/workspace",
                    "Source": str(ws_a),
                    "Type": "bind",
                }
            ]
        )
        inspect_b = json.dumps(
            [
                {
                    "Destination": "/workspace",
                    "Source": str(ws_b),
                    "Type": "bind",
                }
            ]
        )
        monkeypatch.setattr(
            prune.subprocess,
            "run",
            self._fake_run(
                {
                    (
                        "podman",
                        "ps",
                        "-a",
                        "--format",
                        "{{.Names}}",
                    ): "yolo-a-12345678\nyolo-b-87654321\nnot-a-yolo\n",
                    (
                        "podman",
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        "yolo-a-12345678",
                    ): inspect_a,
                    (
                        "podman",
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        "yolo-b-87654321",
                    ): inspect_b,
                }
            ),
        )
        result = prune._find_yolo_workspaces("podman")
        assert set(result) == {ws_a.resolve(), ws_b.resolve()}

    def test_ignores_non_yolo_containers(self, monkeypatch):
        """Other containers on the user's machine must not be touched."""
        monkeypatch.setattr(
            prune.subprocess,
            "run",
            self._fake_run(
                {
                    (
                        "podman",
                        "ps",
                        "-a",
                        "--format",
                        "{{.Names}}",
                    ): "unrelated-db\nsome-app\n",
                }
            ),
        )
        assert prune._find_yolo_workspaces("podman") == []

    def test_tolerates_missing_runtime(self, monkeypatch):
        """podman not installed → return empty, not crash."""

        def raising(*a, **kw):
            raise FileNotFoundError("podman")

        monkeypatch.setattr(prune.subprocess, "run", raising)
        assert prune._find_yolo_workspaces("podman") == []

    def test_tolerates_malformed_inspect_output(self, monkeypatch):
        """A container that reports garbage from `inspect` must be
        skipped silently — missing a single workspace is strictly
        better than crashing the whole prune."""
        monkeypatch.setattr(
            prune.subprocess,
            "run",
            self._fake_run(
                {
                    (
                        "podman",
                        "ps",
                        "-a",
                        "--format",
                        "{{.Names}}",
                    ): "yolo-broken-abc\n",
                    (
                        "podman",
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        "yolo-broken-abc",
                    ): "this is not json",
                }
            ),
        )
        assert prune._find_yolo_workspaces("podman") == []

    def test_deduplicates_same_workspace_across_containers(self, monkeypatch, tmp_path):
        """Two containers for the same workspace (rare but possible
        mid-restart) must collapse to one path in the output."""
        ws = tmp_path / "shared"
        ws.mkdir()
        mounts = json.dumps(
            [{"Destination": "/workspace", "Source": str(ws), "Type": "bind"}]
        )
        monkeypatch.setattr(
            prune.subprocess,
            "run",
            self._fake_run(
                {
                    (
                        "podman",
                        "ps",
                        "-a",
                        "--format",
                        "{{.Names}}",
                    ): "yolo-x-1\nyolo-x-2\n",
                    (
                        "podman",
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        "yolo-x-1",
                    ): mounts,
                    (
                        "podman",
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        "yolo-x-2",
                    ): mounts,
                }
            ),
        )
        assert prune._find_yolo_workspaces("podman") == [ws.resolve()]


# ---------------------------------------------------------------------------
# File discovery under .yolo/home — what we'll dedupe
# ---------------------------------------------------------------------------


class TestWalkDedupableFiles:
    """`_walk_dedupable_files(workspaces)` yields every regular file
    under each workspace's ``.yolo/home/{npm-global,local,go}`` as a
    (path, size) pair.  Hashing is deferred to dedup pass (only
    pay the I/O cost for files that share a size)."""

    def _make_ws(self, root: Path, name: str, files: dict[str, bytes]) -> Path:
        ws = root / name
        home = ws / ".yolo" / "home"
        for rel, data in files.items():
            p = home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return ws

    def test_yields_files_under_dedupable_subtrees(self, tmp_path):
        ws = self._make_ws(
            tmp_path,
            "a",
            {
                "npm-global/bin/claude": b"x" * 10,
                "local/share/foo.so": b"y" * 20,
                "go/bin/tool": b"z" * 30,
            },
        )
        entries = list(prune._walk_dedupable_files([ws]))
        sizes = sorted(e.size for e in entries)
        assert sizes == [10, 20, 30]

    def test_skips_symlinks(self, tmp_path):
        """Hardlinks onto symlink targets is a footgun — never dedupe
        a symlink itself.  Its target is handled when the walker
        visits it directly."""
        ws = self._make_ws(tmp_path, "a", {"npm-global/real": b"hi"})
        (ws / ".yolo" / "home" / "npm-global" / "link").symlink_to(
            ws / ".yolo" / "home" / "npm-global" / "real"
        )
        entries = list(prune._walk_dedupable_files([ws]))
        names = sorted(e.path.name for e in entries)
        assert names == ["real"]

    def test_skips_zero_byte_files(self, tmp_path):
        """Empty files can't save bytes and hardlinking them just
        increases inode churn.  Filter them out."""
        ws = self._make_ws(
            tmp_path,
            "a",
            {"npm-global/empty": b"", "npm-global/data": b"hi"},
        )
        names = sorted(e.path.name for e in prune._walk_dedupable_files([ws]))
        assert names == ["data"]

    def test_ignores_other_subdirs(self, tmp_path):
        """We only dedupe the known-safe tool-state subtrees.  Agent
        config dirs (.claude/ etc.) are deliberately excluded — they
        contain per-workspace state we shouldn't share."""
        ws = self._make_ws(
            tmp_path,
            "a",
            {
                "npm-global/bin/a": b"aaa",
                "claude/settings.json": b"not shared",
            },
        )
        names = sorted(e.path.name for e in prune._walk_dedupable_files([ws]))
        assert names == ["a"]

    def test_multiple_workspaces_contribute(self, tmp_path):
        ws_a = self._make_ws(tmp_path, "a", {"npm-global/x": b"data"})
        ws_b = self._make_ws(tmp_path, "b", {"npm-global/x": b"data"})
        entries = list(prune._walk_dedupable_files([ws_a, ws_b]))
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Hardlink dedup — the core win
# ---------------------------------------------------------------------------


class TestHardlinkDuplicateFiles:
    """``_hardlink_duplicate_files(entries, *, apply)`` groups entries
    by (size, sha256), and for each group with ≥2 files hardlinks the
    rest to the first.  Returns ``(bytes_saved, links_made)``.  With
    ``apply=False`` (dry-run default), the fs is untouched but the
    returned numbers reflect what WOULD happen.
    """

    def _entry(self, p: Path) -> "prune.DedupEntry":
        return prune.DedupEntry(path=p, size=p.stat().st_size)

    def test_identical_files_get_hardlinked(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        payload = b"identical-payload" * 1000  # 17 KB
        a.write_bytes(payload)
        b.write_bytes(payload)
        assert a.stat().st_ino != b.stat().st_ino  # different inodes

        bytes_saved, links_made = prune._hardlink_duplicate_files(
            [self._entry(a), self._entry(b)], apply=True
        )

        assert a.stat().st_ino == b.stat().st_ino  # now shared inode
        assert a.read_bytes() == payload  # content preserved
        assert b.read_bytes() == payload
        assert bytes_saved == len(payload)
        assert links_made == 1

    def test_dry_run_reports_but_does_not_link(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        payload = b"xyz" * 500
        a.write_bytes(payload)
        b.write_bytes(payload)
        before_ino = (a.stat().st_ino, b.stat().st_ino)

        bytes_saved, links_made = prune._hardlink_duplicate_files(
            [self._entry(a), self._entry(b)], apply=False
        )

        assert (a.stat().st_ino, b.stat().st_ino) == before_ino  # unchanged
        assert bytes_saved == len(payload)
        assert links_made == 1

    def test_different_content_untouched(self, tmp_path):
        """Same size, different bytes → must NOT be linked (content
        would silently corrupt)."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_bytes(b"AAAAAAAA")
        b.write_bytes(b"BBBBBBBB")  # same size, different content
        before_ino = (a.stat().st_ino, b.stat().st_ino)

        bytes_saved, links_made = prune._hardlink_duplicate_files(
            [self._entry(a), self._entry(b)], apply=True
        )

        assert (a.stat().st_ino, b.stat().st_ino) == before_ino
        assert bytes_saved == 0
        assert links_made == 0
        assert a.read_bytes() == b"AAAAAAAA"
        assert b.read_bytes() == b"BBBBBBBB"

    def test_already_hardlinked_is_noop(self, tmp_path):
        """Two paths that already share an inode shouldn't count as a
        new dedup and shouldn't be re-linked (wasted work)."""
        a = tmp_path / "a"
        a.write_bytes(b"content" * 100)
        b = tmp_path / "b"
        os.link(a, b)
        assert a.stat().st_ino == b.stat().st_ino

        bytes_saved, links_made = prune._hardlink_duplicate_files(
            [self._entry(a), self._entry(b)], apply=True
        )

        assert bytes_saved == 0
        assert links_made == 0

    def test_three_way_dedup_counts_two_saves(self, tmp_path):
        """Three identical files → two hardlinks made, bytes_saved =
        2 × filesize (one canonical copy kept)."""
        payload = b"shared" * 1000  # 6 KB
        files = [tmp_path / n for n in ("a", "b", "c")]
        for f in files:
            f.write_bytes(payload)

        bytes_saved, links_made = prune._hardlink_duplicate_files(
            [self._entry(f) for f in files], apply=True
        )

        assert bytes_saved == 2 * len(payload)
        assert links_made == 2
        assert len({f.stat().st_ino for f in files}) == 1

    def test_tolerates_cross_device_link_error(self, tmp_path, monkeypatch):
        """``os.link`` across device boundaries raises OSError(EXDEV).
        The dedup pass must log + continue, not crash the whole run."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_bytes(b"data" * 100)
        b.write_bytes(b"data" * 100)

        real_link = os.link
        calls = {"n": 0}

        def fake_link(src, dst):
            calls["n"] += 1
            raise OSError(18, "cross-device link not permitted")

        monkeypatch.setattr(prune.os, "link", fake_link)

        # Should return bytes_saved=0 (nothing actually got linked) but not raise.
        bytes_saved, links_made = prune._hardlink_duplicate_files(
            [self._entry(a), self._entry(b)], apply=True
        )
        assert bytes_saved == 0
        assert links_made == 0
        assert calls["n"] >= 1
        # Files still separate, still intact.
        assert a.read_bytes() == b.read_bytes() == b"data" * 100
        # Restore for teardown.
        monkeypatch.setattr(prune.os, "link", real_link)


# ---------------------------------------------------------------------------
# Stopped-container prune (yolo-* only)
# ---------------------------------------------------------------------------


class TestPruneStoppedContainers:
    """``_prune_stopped_containers(runtime, *, apply)`` lists stopped
    containers whose name starts with ``yolo-`` and removes them.
    Other containers on the user's machine are left alone.
    """

    def _fake_run_factory(self, ps_output: str):
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:3] == ["podman", "ps", "-a"]:
                return MagicMock(returncode=0, stdout=ps_output, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return runner, calls

    def test_dry_run_lists_but_does_not_remove(self, monkeypatch):
        runner, calls = self._fake_run_factory(
            "yolo-old-abc exited\nyolo-live-def running\nother-thing exited\n"
        )
        monkeypatch.setattr(prune.subprocess, "run", runner)

        removed = prune._prune_stopped_containers("podman", apply=False)

        assert removed == ["yolo-old-abc"]
        # No rm invocations — dry-run only does the ps listing.
        assert not any(c[:3] == ["podman", "rm"] for c in calls)

    def test_apply_invokes_rm(self, monkeypatch):
        runner, calls = self._fake_run_factory(
            "yolo-old-abc exited\nyolo-live-def running\n"
        )
        monkeypatch.setattr(prune.subprocess, "run", runner)

        removed = prune._prune_stopped_containers("podman", apply=True)

        assert removed == ["yolo-old-abc"]
        assert ["podman", "rm", "yolo-old-abc"] in calls

    def test_ignores_non_yolo_containers(self, monkeypatch):
        """Only yolo-* names — must never touch user's other
        stopped containers."""
        runner, calls = self._fake_run_factory("my-database exited\nsome-app exited\n")
        monkeypatch.setattr(prune.subprocess, "run", runner)

        removed = prune._prune_stopped_containers("podman", apply=True)

        assert removed == []
        assert not any(
            "my-database" in " ".join(c) or "some-app" in " ".join(c) for c in calls
        )

    def test_tolerates_missing_runtime(self, monkeypatch):
        def raising(*a, **kw):
            raise FileNotFoundError("podman")

        monkeypatch.setattr(prune.subprocess, "run", raising)
        assert prune._prune_stopped_containers("podman", apply=True) == []


# ---------------------------------------------------------------------------
# Old-image prune (keep latest N)
# ---------------------------------------------------------------------------


class TestPruneOldImages:
    """``_prune_old_images(runtime, keep=N, *, apply)`` — list yolo-jail
    images sorted newest-first, keep the latest ``keep``, rm the rest.
    Tests use canned podman images output.
    """

    def _fake_run_factory(self, images_output: str):
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[:3] == ["podman", "images", "--format"]:
                return MagicMock(returncode=0, stdout=images_output, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        return runner, calls

    def test_keeps_when_under_threshold(self, monkeypatch):
        """Fewer images than ``keep`` → nothing to do."""
        runner, calls = self._fake_run_factory(
            "id1 yolo-jail:latest 2026-04-20T00:00:00Z\n"
            "id2 yolo-jail:0.4.2 2026-04-19T00:00:00Z\n"
        )
        monkeypatch.setattr(prune.subprocess, "run", runner)

        removed = prune._prune_old_images("podman", keep=3, apply=True)

        assert removed == []
        assert not any(c[:3] == ["podman", "rmi"] for c in calls)

    def test_removes_oldest_when_over_threshold(self, monkeypatch):
        runner, calls = self._fake_run_factory(
            "new-id yolo-jail:latest 2026-04-20T00:00:00Z\n"
            "mid-id yolo-jail:0.4.2 2026-04-15T00:00:00Z\n"
            "old-id yolo-jail:0.4.1 2026-04-10T00:00:00Z\n"
            "older   yolo-jail:0.4.0 2026-04-01T00:00:00Z\n"
        )
        monkeypatch.setattr(prune.subprocess, "run", runner)

        removed = prune._prune_old_images("podman", keep=2, apply=True)

        assert removed == ["old-id", "older"]
        # Newest two (new-id, mid-id) untouched.
        rmi_cmds = [c for c in calls if c[:3] == ["podman", "rmi", "-f"]]
        assert len(rmi_cmds) == 2

    def test_dry_run_does_not_remove(self, monkeypatch):
        runner, calls = self._fake_run_factory(
            "a yolo-jail:latest 2026-04-20T00:00:00Z\n"
            "b yolo-jail:old 2026-04-10T00:00:00Z\n"
        )
        monkeypatch.setattr(prune.subprocess, "run", runner)

        removed = prune._prune_old_images("podman", keep=1, apply=False)

        assert removed == ["b"]
        assert not any(c[:3] == ["podman", "rmi", "-f"] for c in calls)


# ---------------------------------------------------------------------------
# Disk-usage report
# ---------------------------------------------------------------------------


class TestDiskUsageReport:
    """``_disk_usage_report(workspaces, *, global_storage)`` returns a
    dict with per-category byte totals.  Used by both the prune CLI
    and the doctor threshold check.
    """

    def test_sums_workspaces_and_global_storage(self, tmp_path):
        # Fake GLOBAL_STORAGE layout
        gs = tmp_path / "yolo-jail"
        (gs / "cache").mkdir(parents=True)
        (gs / "cache" / "f").write_bytes(b"x" * 100)
        (gs / "home").mkdir()
        (gs / "home" / "f").write_bytes(b"y" * 50)
        # Two workspaces, each with .yolo/home tree
        ws1 = tmp_path / "ws1"
        (ws1 / ".yolo" / "home" / "npm-global").mkdir(parents=True)
        (ws1 / ".yolo" / "home" / "npm-global" / "f").write_bytes(b"a" * 200)
        ws2 = tmp_path / "ws2"
        (ws2 / ".yolo" / "home" / "local").mkdir(parents=True)
        (ws2 / ".yolo" / "home" / "local" / "f").write_bytes(b"b" * 300)

        report = prune._disk_usage_report(workspaces=[ws1, ws2], global_storage=gs)

        assert report["global_storage"] == 150
        # workspaces is reported as a sum across all known workspaces.
        assert report["workspaces"] == 500
        # total is the grand sum.
        assert report["total"] == 650

    def test_missing_paths_contribute_zero(self, tmp_path):
        """Never-run yolo → GLOBAL_STORAGE may not exist yet.
        Don't crash; just report 0."""
        report = prune._disk_usage_report(
            workspaces=[], global_storage=tmp_path / "missing"
        )
        assert report == {"global_storage": 0, "workspaces": 0, "total": 0}


# ---------------------------------------------------------------------------
# `yolo prune` CLI integration
# ---------------------------------------------------------------------------


class TestPruneCommand:
    """End-to-end of the typer command.  Real filesystem, mocked
    subprocess so we don't touch the host's real podman."""

    def _invoke(self, args: list[str]):
        from typer.testing import CliRunner
        from src.cli import app

        return CliRunner().invoke(app, ["prune", *args])

    def test_dry_run_is_default(self, monkeypatch, tmp_path):
        """`yolo prune` with no flags must NOT mutate the filesystem."""
        ws = tmp_path / "ws"
        home = ws / ".yolo" / "home" / "npm-global"
        home.mkdir(parents=True)
        a = home / "dup-a"
        b = home / "dup-b"
        payload = b"same" * 200
        a.write_bytes(payload)
        b.write_bytes(payload)
        ino_before = (a.stat().st_ino, b.stat().st_ino)

        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [ws])
        monkeypatch.setattr(
            prune, "_prune_stopped_containers", lambda runtime, *, apply: []
        )
        monkeypatch.setattr(
            prune, "_prune_old_images", lambda runtime, *, keep, apply: []
        )

        result = self._invoke([])

        assert result.exit_code == 0
        # Dry-run banner and hint visible in output.
        assert "DRY-RUN" in result.output
        assert "--apply" in result.output
        # Files untouched — hardlink NOT made.
        assert (a.stat().st_ino, b.stat().st_ino) == ino_before

    def test_apply_flag_executes_dedup(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        home = ws / ".yolo" / "home" / "npm-global"
        home.mkdir(parents=True)
        a = home / "dup-a"
        b = home / "dup-b"
        payload = b"same" * 500
        a.write_bytes(payload)
        b.write_bytes(payload)
        assert a.stat().st_ino != b.stat().st_ino  # sanity

        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [ws])
        monkeypatch.setattr(
            prune, "_prune_stopped_containers", lambda runtime, *, apply: []
        )
        monkeypatch.setattr(
            prune, "_prune_old_images", lambda runtime, *, keep, apply: []
        )

        result = self._invoke(["--apply"])

        assert result.exit_code == 0
        # Hardlink actually created.
        assert a.stat().st_ino == b.stat().st_ino
        assert "Reclaimed" in result.output

    def test_no_hardlink_skips_dedup(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        home = ws / ".yolo" / "home" / "npm-global"
        home.mkdir(parents=True)
        (home / "a").write_bytes(b"x" * 100)
        (home / "b").write_bytes(b"x" * 100)

        called = {"walk": 0}
        real_walk = prune._walk_dedupable_files

        def tracked_walk(ws_list):
            called["walk"] += 1
            return real_walk(ws_list)

        monkeypatch.setattr(prune, "_walk_dedupable_files", tracked_walk)
        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [ws])
        monkeypatch.setattr(
            prune, "_prune_stopped_containers", lambda runtime, *, apply: []
        )
        monkeypatch.setattr(
            prune, "_prune_old_images", lambda runtime, *, keep, apply: []
        )

        result = self._invoke(["--no-hardlink", "--apply"])

        assert result.exit_code == 0
        # Dedup pass must not have run.
        assert called["walk"] == 0

    def test_keep_images_flag_passes_through(self, monkeypatch):
        seen = {"keep": None}

        def fake_prune_images(runtime, *, keep, apply):
            seen["keep"] = keep
            return []

        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [])
        monkeypatch.setattr(
            prune, "_prune_stopped_containers", lambda runtime, *, apply: []
        )
        monkeypatch.setattr(prune, "_prune_old_images", fake_prune_images)

        result = self._invoke(["--keep-images", "5"])

        assert result.exit_code == 0
        assert seen["keep"] == 5


# ---------------------------------------------------------------------------
# `_check_disk_usage` — the lifecycle nudge
# ---------------------------------------------------------------------------


class TestDoctorDiskUsageCheck:
    """The doctor threshold check surfaces total yolo-jail disk use
    and nudges toward ``yolo prune`` when it crosses a threshold."""

    def _check_import(self):
        import sys
        from pathlib import Path as _P

        root = _P(__file__).parent.parent / "src"
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from cli import _check_disk_usage  # type: ignore

        return _check_disk_usage

    def test_under_threshold_emits_ok(self, monkeypatch, tmp_path):
        _check = self._check_import()
        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [])
        monkeypatch.setattr(
            prune,
            "_disk_usage_report",
            lambda *, workspaces, global_storage: {
                "global_storage": 1_000_000_000,  # 1 GiB
                "workspaces": 0,
                "total": 1_000_000_000,
            },
        )
        monkeypatch.delenv("YOLO_VERSION", raising=False)

        calls: list[tuple] = []
        _check(
            lambda m, *a, **kw: calls.append(("ok", m)),
            lambda *a, **kw: calls.append(("warn", a[0] if a else "")),
            lambda *a, **kw: calls.append(("fail", a[0] if a else "")),
            threshold_gb=10.0,
        )
        assert calls, "expected at least one doctor callback"
        assert calls[0][0] == "ok"
        assert all(kind != "warn" for kind, _ in calls)

    def test_over_threshold_warns_with_prune_hint(self, monkeypatch, tmp_path):
        _check = self._check_import()
        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [])
        monkeypatch.setattr(
            prune,
            "_disk_usage_report",
            lambda *, workspaces, global_storage: {
                "global_storage": 20 * (1024**3),  # 20 GiB
                "workspaces": 5 * (1024**3),  # 5 GiB
                "total": 25 * (1024**3),
            },
        )
        monkeypatch.delenv("YOLO_VERSION", raising=False)

        warnings: list[tuple] = []
        _check(
            lambda *a, **kw: None,
            lambda msg, note="", *a, **kw: warnings.append(("warn", msg, note)),
            lambda *a, **kw: None,
            threshold_gb=15.0,
        )
        assert warnings, "expected a warn over threshold"
        msg = warnings[0][1]
        note = warnings[0][2]
        assert "yolo-jail disk usage" in msg
        assert "yolo prune" in note

    def test_skips_inside_jail(self, monkeypatch):
        """Inside a jail the check has no visibility into the real
        host storage — skip entirely, same pattern the loophole
        check uses."""
        _check = self._check_import()
        monkeypatch.setenv("YOLO_VERSION", "test")
        calls: list[tuple] = []
        _check(
            lambda m, *a, **kw: calls.append(("ok", m)),
            lambda *a, **kw: calls.append(("warn", "")),
            lambda *a, **kw: calls.append(("fail", "")),
        )
        # One ok explaining the skip, no warn/fail.
        assert len(calls) == 1
        assert calls[0][0] == "ok"
        assert "Inside jail" in calls[0][1]

    def test_config_overrides_threshold(self, monkeypatch):
        """A user-set ``prune.warn_threshold_gb`` in config must take
        precedence over the builtin default."""
        _check = self._check_import()
        monkeypatch.setattr(prune, "_find_yolo_workspaces", lambda runtime: [])
        monkeypatch.setattr(
            prune,
            "_disk_usage_report",
            lambda *, workspaces, global_storage: {
                "global_storage": 8 * (1024**3),  # 8 GiB
                "workspaces": 0,
                "total": 8 * (1024**3),
            },
        )
        monkeypatch.delenv("YOLO_VERSION", raising=False)

        # Default threshold is 15 GiB → 8 GiB is OK.  Lower it via
        # config to 5 GiB → 8 GiB now triggers a warn.
        warnings: list[str] = []
        _check(
            lambda *a, **kw: None,
            lambda msg, note="", *a, **kw: warnings.append(msg),
            lambda *a, **kw: None,
            config={"prune": {"warn_threshold_gb": 5}},
        )
        assert warnings, "custom threshold must cause a warn"
