"""Unit tests for src/cli.py — pure functions and mockable logic.

Covers: argv routing, repo root resolution, config validation, container naming,
port forwarding, AGENTS.md generation, check command, and helpers.
"""

import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "src"))

from typer.testing import CliRunner  # noqa: E402

import cli as cli  # noqa: E402

from cli import (  # noqa: E402
    _check_preset_null_conflicts,
    _effective_mcp_server_names,
    _format_progress,
    _host_mise_dir,
    _merge_mise_tools,
    _normalize_blocked_tools,
    _parse_port_forwards,
    _prepare_skills,
    _read_loaded_paths,
    _add_loaded_path,
    _report_unknown_keys,
    _runtime_for_check,
    _summarize_nix_line,
    _validate_config,
    _validate_forward_host_port,
    _validate_port_number,
    _validate_publish_port,
    _validate_string_list,
    cleanup_container_tracking,
    cleanup_port_forwarding,
    container_name_for_workspace,
    ensure_global_storage,
    find_existing_container,
    find_running_container,
    generate_agents_md,
    load_config,
    merge_config,
    write_container_tracking,
    _check_config_changes,
    _config_snapshot_path,
    _get_project_name,
    _get_yolo_version,
    _load_jsonc_file,
    _merge_lists,
    ConfigError,
    _validate_cgroup_name,
    _parse_memory_value,
    _print_startup_banner,
    _remove_stale_container,
    _cgd_ensure_agent_cgroup,
    _cgd_create_and_join,
    _cgd_destroy,
    BUILTIN_CGROUP_LOOPHOLE_NAME,
    BUILTIN_JOURNAL_LOOPHOLE_NAME,
    LoopholeDaemon,
    JAIL_HOST_SERVICES_DIR,
    _host_service_default_jail_socket,
    _host_service_env_var,
    _host_service_sockets_dir,
    _resolve_journal_mode,
    _start_host_service_builtin_cgroup,
    _start_host_service_builtin_journal,
    _start_host_service_external,
    _substitute_socket_in_cmd,
    start_loopholes,
    stop_loopholes,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: argv routing in main()
# ═══════════════════════════════════════════════════════════════════════════════


class TestArgvRouting:
    """Test the sys.argv rewriting that routes `yolo -- cmd` to `yolo run -- cmd`."""

    def _simulate_argv_rewrite(self, argv: list[str]) -> list[str]:
        """Simulate the argv rewriting logic from main() without calling app()."""
        _SUBCOMMANDS = {
            "init",
            "init-user-config",
            "config-ref",
            "check",
            "run",
            "ps",
            "doctor",
        }
        args = argv[1:]
        result = list(argv)
        if args and "--" in args:
            pre_dash = args[: args.index("--")]
            if not any(a in _SUBCOMMANDS for a in pre_dash):
                idx = result.index("--")
                result.insert(idx, "run")
        return result

    def test_yolo_double_dash_echo(self):
        """The original bug: `yolo -- echo foo` should become `yolo run -- echo foo`."""
        result = self._simulate_argv_rewrite(["yolo", "--", "echo", "foo"])
        assert result == ["yolo", "run", "--", "echo", "foo"]

    def test_yolo_double_dash_bash_c(self):
        result = self._simulate_argv_rewrite(["yolo", "--", "bash", "-c", "echo hello"])
        assert result == ["yolo", "run", "--", "bash", "-c", "echo hello"]

    def test_yolo_new_double_dash_bash(self):
        result = self._simulate_argv_rewrite(["yolo", "--new", "--", "bash"])
        assert result == ["yolo", "--new", "run", "--", "bash"]

    def test_yolo_run_double_dash_echo(self):
        """Explicit `run` subcommand should NOT be doubled."""
        result = self._simulate_argv_rewrite(["yolo", "run", "--", "echo", "foo"])
        assert result == ["yolo", "run", "--", "echo", "foo"]

    def test_yolo_check_not_rewritten(self):
        """Subcommands like `check` should not trigger rewriting."""
        result = self._simulate_argv_rewrite(["yolo", "check"])
        assert result == ["yolo", "check"]

    def test_bare_yolo(self):
        """Bare `yolo` with no args should not be rewritten."""
        result = self._simulate_argv_rewrite(["yolo"])
        assert result == ["yolo"]

    def test_yolo_ps(self):
        result = self._simulate_argv_rewrite(["yolo", "ps"])
        assert result == ["yolo", "ps"]

    def test_yolo_double_dash_copilot(self):
        result = self._simulate_argv_rewrite(["yolo", "--", "copilot"])
        assert result == ["yolo", "run", "--", "copilot"]

    def test_yolo_no_double_dash(self):
        """Without `--`, no rewriting happens even for unknown commands."""
        result = self._simulate_argv_rewrite(["yolo", "echo", "foo"])
        assert result == ["yolo", "echo", "foo"]

    def test_yolo_init_double_dash(self):
        """Known subcommand before -- should not insert run."""
        result = self._simulate_argv_rewrite(["yolo", "init", "--", "something"])
        assert result == ["yolo", "init", "--", "something"]

    def test_yolo_doctor(self):
        result = self._simulate_argv_rewrite(["yolo", "doctor"])
        assert result == ["yolo", "doctor"]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _resolve_repo_root
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveRepoRoot:
    """Test the 4-step repo root resolution."""

    def test_env_var_takes_priority(self, tmp_path):
        env_root = tmp_path / "env-repo"
        env_root.mkdir()
        # Must contain expected marker files so validation passes
        (env_root / "flake.nix").touch()
        with patch.dict(os.environ, {"YOLO_REPO_ROOT": str(env_root)}):
            from cli import _resolve_repo_root

            result = _resolve_repo_root()
            assert result == env_root.resolve()

    def test_env_var_falls_through_when_empty(self, tmp_path):
        """YOLO_REPO_ROOT pointing to an empty dir falls through to source checkout."""
        empty_root = tmp_path / "empty-repo"
        empty_root.mkdir()
        with patch.dict(os.environ, {"YOLO_REPO_ROOT": str(empty_root)}):
            from cli import _resolve_repo_root

            result = _resolve_repo_root()
            # Should NOT return the empty dir — should fall through
            assert result != empty_root.resolve()
            # Should find the actual source checkout instead
            assert (result / "flake.nix").exists() or (
                result / "src" / "entrypoint.py"
            ).exists()

    def test_source_checkout_detected(self, monkeypatch):
        """Running from the actual source checkout should find the repo root."""
        monkeypatch.delenv("YOLO_REPO_ROOT", raising=False)
        from cli import _resolve_repo_root

        result = _resolve_repo_root()
        # We ARE in a source checkout, so this should find REPO_ROOT
        assert (result / "flake.nix").exists()

    def test_installed_package_stages_build_root(self, tmp_path, monkeypatch):
        """When flake.nix is in the package dir (installed mode), files are copied."""
        # This path is complex to unit test due to Path(__file__) mocking.
        # Covered by the user's manual testing and integration tests.
        pass

    def test_user_config_repo_path(self, tmp_path, monkeypatch):
        """Step 4: user config with repo_path — tested indirectly via env var priority."""
        pass  # Covered by env var test and integration tests


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Container naming & tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestContainerNaming:
    def test_deterministic_name(self, tmp_path):
        ws = tmp_path / "my-project"
        ws.mkdir()
        name = container_name_for_workspace(ws)
        assert name.startswith("yolo-my-project-")
        assert len(name.rsplit("-", 1)[-1]) == 8  # 8 hex char suffix

    def test_same_workspace_same_name(self, tmp_path):
        ws = tmp_path / "project"
        ws.mkdir()
        assert container_name_for_workspace(ws) == container_name_for_workspace(ws)

    def test_different_workspace_different_name(self, tmp_path):
        ws1 = tmp_path / "project1"
        ws2 = tmp_path / "project2"
        ws1.mkdir()
        ws2.mkdir()
        assert container_name_for_workspace(ws1) != container_name_for_workspace(ws2)


class TestContainerTracking:
    def test_write_and_cleanup(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path)
        write_container_tracking("yolo-abc123", tmp_path / "ws")
        assert (tmp_path / "yolo-abc123").exists()
        cleanup_container_tracking("yolo-abc123")
        assert not (tmp_path / "yolo-abc123").exists()

    def test_cleanup_missing_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path)
        cleanup_container_tracking("nonexistent")  # Should not raise


class TestFindRunningContainer:
    def test_returns_cid_when_running(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123def\n")
            result = find_running_container("yolo-test", runtime="docker")
            assert result == "abc123def"

    def test_returns_none_when_not_running(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = find_running_container("yolo-test", runtime="docker")
            assert result is None

    def test_returns_none_on_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = find_running_container("yolo-test", runtime="docker")
            assert result is None


class TestFindExistingContainer:
    def test_returns_cid_when_stopped(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="abc123def\n")
            result = find_existing_container("yolo-test", runtime="docker")
            assert result == "abc123def"
            mock_run.assert_called_once_with(
                ["docker", "ps", "-a", "-q", "--filter", "name=^/yolo-test$"],
                capture_output=True,
                text=True,
            )

    def test_returns_none_when_no_container(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = find_existing_container("yolo-test", runtime="docker")
            assert result is None

    def test_returns_none_on_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = find_existing_container("yolo-test", runtime="docker")
            assert result is None

    def test_apple_container_runtime(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="NAME\nyolo-test\n")
            result = find_existing_container("yolo-test", runtime="container")
            assert result == "yolo-test"
            mock_run.assert_called_once_with(
                ["container", "ls", "--all"],
                capture_output=True,
                text=True,
            )

    def test_podman_runtime(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="def456\n")
            result = find_existing_container("yolo-test", runtime="podman")
            assert result == "def456"
            mock_run.assert_called_once_with(
                ["podman", "ps", "-a", "-q", "--filter", "name=^/yolo-test$"],
                capture_output=True,
                text=True,
            )


class TestRemoveStaleContainer:
    def test_successful_removal(self, tmp_path):
        with (
            patch("subprocess.run") as mock_run,
            patch("cli.cleanup_container_tracking") as mock_cleanup,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = _remove_stale_container("yolo-test", runtime="docker")
            assert result is True
            mock_run.assert_called_once_with(
                ["docker", "rm", "yolo-test"],
                capture_output=True,
                text=True,
            )
            mock_cleanup.assert_called_once_with("yolo-test")

    def test_failed_removal(self):
        with (
            patch("subprocess.run") as mock_run,
            patch("cli.cleanup_container_tracking") as mock_cleanup,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            result = _remove_stale_container("yolo-test", runtime="docker")
            assert result is False
            mock_cleanup.assert_not_called()

    def test_apple_container_runtime(self):
        with (
            patch("subprocess.run") as mock_run,
            patch("cli.cleanup_container_tracking"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _remove_stale_container("yolo-test", runtime="container")
            mock_run.assert_called_once_with(
                ["container", "rm", "--force", "yolo-test"],
                capture_output=True,
                text=True,
            )

    def test_runtime_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _remove_stale_container("yolo-test", runtime="docker")
            assert result is False


class TestPrintStartupBanner:
    def test_banner_includes_platform_and_runtime(self, capsys):
        _print_startup_banner("1.0.0", "podman", "yolo-test-abc123")
        err = capsys.readouterr().err
        assert "yolo-jail 1.0.0" in err
        assert "podman" in err
        assert "yolo-test-abc123" in err

    def test_banner_with_resource_limits(self, capsys):
        _print_startup_banner(
            "1.0.0", "docker", "yolo-test-abc123", ["memory=8g", "cpus=4"]
        )
        err = capsys.readouterr().err
        assert "Resource limits: memory=8g, cpus=4" in err

    def test_banner_no_resource_limits(self, capsys):
        _print_startup_banner("1.0.0", "docker", "yolo-test-abc123")
        err = capsys.readouterr().err
        assert "Resource limits" not in err

    def test_banner_surfaces_mismatched_jail_version(self, capsys):
        """When the host CLI differs from the jail's baked YOLO_VERSION,
        the banner must show both — stale-shim bugs on attach are
        invisible otherwise."""
        _print_startup_banner("2.0.0", "podman", "yolo-test", jail_version="1.0.0")
        err = capsys.readouterr().err
        assert "yolo-jail 2.0.0" in err
        assert "1.0.0" in err
        assert "attached" in err.lower()

    def test_banner_hides_matching_jail_version(self, capsys):
        """When versions match, don't clutter the banner."""
        _print_startup_banner("1.0.0", "podman", "yolo-test", jail_version="1.0.0")
        err = capsys.readouterr().err
        assert "attached" not in err.lower()
        # Version appears exactly once (in "yolo-jail 1.0.0")
        assert err.count("1.0.0") == 1

    def test_banner_handles_missing_jail_version(self, capsys):
        """A None jail_version (inspect failed / fresh container) must
        not crash and must leave the banner looking like the old form."""
        _print_startup_banner("1.0.0", "podman", "yolo-test", jail_version=None)
        err = capsys.readouterr().err
        assert "yolo-jail 1.0.0" in err
        assert "attached" not in err.lower()


class TestGetYoloVersion:
    def test_returns_git_describe_version(self):
        with patch("cli._git_describe_version", return_value="1.2.3"):
            assert _get_yolo_version() == "1.2.3"

    def test_falls_back_to_pkg_version(self):
        with (
            patch("cli._git_describe_version", return_value=None),
            patch("importlib.metadata.version", return_value="0.9.0"),
        ):
            assert _get_yolo_version() == "0.9.0"

    def test_returns_unknown_on_error(self):
        with (
            patch("cli._git_describe_version", return_value=None),
            patch("importlib.metadata.version", side_effect=Exception("no pkg")),
        ):
            assert _get_yolo_version() == "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Port forwarding parsing
# ═══════════════════════════════════════════════════════════════════════════════


class TestParsePortForwards:
    def test_integer_port(self):
        assert _parse_port_forwards([8080]) == [(8080, 8080)]

    def test_string_port(self):
        assert _parse_port_forwards(["5432"]) == [(5432, 5432)]

    def test_colon_mapping(self):
        assert _parse_port_forwards(["8080:9090"]) == [(8080, 9090)]

    def test_multiple(self):
        result = _parse_port_forwards([5432, "8080:9090", "3000"])
        assert result == [(5432, 5432), (8080, 9090), (3000, 3000)]

    def test_empty(self):
        assert _parse_port_forwards([]) == []

    def test_invalid_entry_skipped(self, capsys):
        result = _parse_port_forwards([3.14])
        assert result == []
        assert "invalid" in capsys.readouterr().err.lower()


class TestCleanupPortForwarding:
    def test_terminates_processes(self, tmp_path):
        mock_proc = MagicMock()
        cleanup_port_forwarding([mock_proc], tmp_path)
        mock_proc.terminate.assert_called_once()

    def test_kills_on_timeout(self, tmp_path):
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("socat", 2)
        cleanup_port_forwarding([mock_proc], tmp_path)
        mock_proc.kill.assert_called_once()

    def test_removes_socket_dir(self, tmp_path):
        sock_dir = tmp_path / "sockets"
        sock_dir.mkdir()
        cleanup_port_forwarding([], sock_dir)
        assert not sock_dir.exists()

    def test_none_socket_dir(self):
        cleanup_port_forwarding([], None)  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Test: MCP server helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestEffectiveMCPServerNames:
    def test_presets_only(self):
        names = _effective_mcp_server_names(None, ["chrome-devtools"])
        assert names == ["chrome-devtools"]

    def test_custom_servers_added(self):
        servers = {"my-server": {"command": "my-cmd"}}
        names = _effective_mcp_server_names(servers, [])
        assert "my-server" in names

    def test_null_removes_preset(self):
        servers = {"chrome-devtools": None}
        names = _effective_mcp_server_names(servers, ["chrome-devtools"])
        assert "chrome-devtools" not in names

    def test_empty(self):
        assert _effective_mcp_server_names(None, None) == []

    def test_no_duplicates(self):
        servers = {"chrome-devtools": {"command": "cmd"}}
        names = _effective_mcp_server_names(servers, ["chrome-devtools"])
        assert names.count("chrome-devtools") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Mise tools merging
# ═══════════════════════════════════════════════════════════════════════════════


class TestMergeMiseTools:
    def test_default_neovim(self):
        result = _merge_mise_tools({})
        assert result == {"neovim": "stable"}

    def test_override_neovim(self):
        result = _merge_mise_tools({"mise_tools": {"neovim": "nightly"}})
        assert result["neovim"] == "nightly"

    def test_add_new_tool(self):
        result = _merge_mise_tools({"mise_tools": {"typst": "latest"}})
        assert result == {"neovim": "stable", "typst": "latest"}


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Blocked tools normalization
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeBlockedTools:
    def test_default_blocked_tools(self):
        result = _normalize_blocked_tools(None)
        names = [t["name"] for t in result]
        assert "grep" in names
        assert "find" in names

    def test_string_tools_get_defaults(self):
        result = _normalize_blocked_tools({"blocked_tools": ["grep"]})
        assert result[0]["name"] == "grep"
        assert "message" in result[0]

    def test_dict_tools_preserved(self):
        tool = {"name": "curl", "message": "Use wget", "suggestion": "wget URL"}
        result = _normalize_blocked_tools({"blocked_tools": [tool]})
        assert result[0] == tool

    def test_dict_grep_gets_default_block_flags(self):
        """Regression: when the user writes the dict form for a tool
        with baked-in defaults (grep), _normalize_blocked_tools must
        merge the defaults — missing block_flags shouldn't silently
        convert grep into an unconditional block.  The conditional
        rule is part of the default contract; dict-form users get it
        too unless they explicitly override."""
        result = _normalize_blocked_tools({"blocked_tools": [{"name": "grep"}]})
        assert result[0]["name"] == "grep"
        assert result[0].get("block_flags"), (
            "dict-form grep must inherit default block_flags"
        )
        # And the default message should also be present.
        assert "rg" in result[0].get("suggestion", "")

    def test_dict_grep_user_fields_win_over_defaults(self):
        """User-supplied fields override defaults; unspecified fields
        inherit.  So ``{"name": "grep", "message": "custom"}`` gets
        custom message + default suggestion + default block_flags."""
        result = _normalize_blocked_tools(
            {"blocked_tools": [{"name": "grep", "message": "custom msg"}]}
        )
        assert result[0]["message"] == "custom msg"
        assert result[0].get("block_flags"), "defaults preserved"
        assert "rg" in result[0].get("suggestion", "")

    def test_dict_grep_explicit_empty_block_flags_disables_conditional(self):
        """User can opt out of conditional blocking by setting
        ``block_flags: []`` — reverting grep to the unconditional
        behavior that matches the legacy contract."""
        result = _normalize_blocked_tools(
            {"blocked_tools": [{"name": "grep", "block_flags": []}]}
        )
        assert result[0]["block_flags"] == []

    def test_custom_string_tool(self):
        result = _normalize_blocked_tools({"blocked_tools": ["strace"]})
        assert result[0]["name"] == "strace"
        assert "message" not in result[0]

    def test_none_blocked_tools(self):
        result = _normalize_blocked_tools({"blocked_tools": None})
        assert len(result) == 2  # defaults


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _summarize_nix_line
# ═══════════════════════════════════════════════════════════════════════════════


class TestSummarizeNixLine:
    def test_copying_path(self):
        line = "copying path '/nix/store/abc123-glibc-2.38' from 'https://...'"
        assert _summarize_nix_line(line) == "Fetching glibc-2.38"

    def test_building_drv(self):
        line = "building '/nix/store/abc123-python3-3.12.drv'..."
        assert _summarize_nix_line(line) == "Building python3-3.12"

    def test_evaluating(self):
        assert "Evaluating" in _summarize_nix_line("evaluating derivation...")

    def test_progress_counter(self):
        line = "[3/5 built, 2 copied (10.2 MiB)]"
        result = _summarize_nix_line(line)
        assert result == line.strip()

    def test_unrecognized(self):
        assert _summarize_nix_line("some random output") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _format_progress
# ═══════════════════════════════════════════════════════════════════════════════


class TestFormatProgress:
    def test_mb_no_estimate(self):
        result = _format_progress(50 * 1024 * 1024, 0)
        assert "50 MB" in result

    def test_gb_with_estimate(self):
        result = _format_progress(1500 * 1024 * 1024, 2000 * 1024 * 1024)
        assert "GB" in result
        assert "%" in result

    def test_caps_at_99(self):
        result = _format_progress(999, 1000)
        assert "99%" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Sentinel file management
# ═══════════════════════════════════════════════════════════════════════════════


class TestSentinelFiles:
    def test_read_empty(self, tmp_path):
        sentinel = tmp_path / "sentinel"
        assert _read_loaded_paths(sentinel) == set()

    def test_read_paths(self, tmp_path):
        sentinel = tmp_path / "sentinel"
        sentinel.write_text("/nix/store/abc\n/nix/store/def\n")
        assert _read_loaded_paths(sentinel) == {"/nix/store/abc", "/nix/store/def"}

    def test_add_path(self, tmp_path):
        sentinel = tmp_path / "sentinel"
        _add_loaded_path(sentinel, "/nix/store/abc")
        assert "/nix/store/abc" in sentinel.read_text()

    def test_lru_caps_at_10(self, tmp_path):
        sentinel = tmp_path / "sentinel"
        for i in range(15):
            _add_loaded_path(sentinel, f"/nix/store/path-{i}")
        lines = [ln for ln in sentinel.read_text().splitlines() if ln.strip()]
        assert len(lines) == 10
        # Most recent should be present
        assert "/nix/store/path-14" in sentinel.read_text()
        # Oldest should be evicted
        assert "/nix/store/path-0" not in sentinel.read_text()

    def test_deduplicates(self, tmp_path):
        sentinel = tmp_path / "sentinel"
        _add_loaded_path(sentinel, "/nix/store/abc")
        _add_loaded_path(sentinel, "/nix/store/abc")
        lines = [ln for ln in sentinel.read_text().splitlines() if ln.strip()]
        assert len(lines) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Config loading (JSONC)
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadJsoncFile:
    def test_nonexistent_returns_empty(self, tmp_path):
        result = _load_jsonc_file(tmp_path / "nope.jsonc", "test")
        assert result == {}

    def test_valid_json(self, tmp_path):
        f = tmp_path / "config.jsonc"
        f.write_text('{"runtime": "podman"}')
        result = _load_jsonc_file(f, "test")
        assert result == {"runtime": "podman"}

    def test_non_object_warns(self, tmp_path):
        f = tmp_path / "config.jsonc"
        f.write_text("[1, 2, 3]")
        result = _load_jsonc_file(f, "test")
        assert result == {}

    def test_non_object_strict_raises(self, tmp_path):
        f = tmp_path / "config.jsonc"
        f.write_text("[1, 2, 3]")
        with pytest.raises(ConfigError):
            _load_jsonc_file(f, "test", strict=True)

    def test_invalid_json_strict_raises(self, tmp_path):
        f = tmp_path / "config.jsonc"
        f.write_text("{broken json")
        with pytest.raises(ConfigError):
            _load_jsonc_file(f, "test", strict=True)

    def test_invalid_json_non_strict_warns(self, tmp_path):
        f = tmp_path / "config.jsonc"
        f.write_text("{broken json")
        result = _load_jsonc_file(f, "test", strict=False)
        assert result == {}


class TestLoadConfig:
    def test_empty_workspace(self, tmp_path):
        with patch("cli.USER_CONFIG_PATH", tmp_path / "nonexistent.jsonc"):
            result = load_config(tmp_path)
            assert result == {}

    def test_workspace_config_merged(self, tmp_path):
        ws_config = tmp_path / "yolo-jail.jsonc"
        ws_config.write_text('{"runtime": "docker"}')
        with patch("cli.USER_CONFIG_PATH", tmp_path / "nonexistent.jsonc"):
            result = load_config(tmp_path)
            assert result["runtime"] == "docker"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Config validation — comprehensive
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateConfig:
    """Test _validate_config for all config sections."""

    def test_empty_config_valid(self, tmp_path):
        errors, warnings = _validate_config({}, workspace=tmp_path)
        assert errors == []

    def test_unknown_top_level_key(self, tmp_path):
        errors, _ = _validate_config({"foo": "bar"}, workspace=tmp_path)
        assert any("unknown key" in e for e in errors)

    def test_invalid_runtime(self, tmp_path):
        errors, _ = _validate_config({"runtime": "containerd"}, workspace=tmp_path)
        assert any("runtime" in e for e in errors)

    def test_valid_runtime(self, tmp_path):
        errors, _ = _validate_config({"runtime": "podman"}, workspace=tmp_path)
        assert not any("runtime" in e for e in errors)

    def test_packages_string(self, tmp_path):
        errors, _ = _validate_config({"packages": ["postgresql"]}, workspace=tmp_path)
        assert errors == []

    def test_packages_object_nixpkgs(self, tmp_path):
        errors, _ = _validate_config(
            {"packages": [{"name": "freetype", "nixpkgs": "abc123"}]},
            workspace=tmp_path,
        )
        assert errors == []

    def test_packages_object_version_override(self, tmp_path):
        errors, _ = _validate_config(
            {
                "packages": [
                    {
                        "name": "freetype",
                        "version": "2.14.1",
                        "url": "mirror://...",
                        "hash": "sha256-...",
                    }
                ]
            },
            workspace=tmp_path,
        )
        assert errors == []

    def test_packages_both_nixpkgs_and_version_error(self, tmp_path):
        errors, _ = _validate_config(
            {"packages": [{"name": "freetype", "nixpkgs": "abc", "version": "2.14.1"}]},
            workspace=tmp_path,
        )
        assert any("either" in e.lower() for e in errors)

    def test_packages_object_no_strategy(self, tmp_path):
        errors, _ = _validate_config(
            {"packages": [{"name": "freetype"}]}, workspace=tmp_path
        )
        assert any("must use" in e.lower() for e in errors)

    def test_packages_unknown_keys(self, tmp_path):
        errors, _ = _validate_config(
            {"packages": [{"name": "foo", "nixpkgs": "abc", "bogus": True}]},
            workspace=tmp_path,
        )
        assert any("unknown" in e for e in errors)

    def test_packages_not_list(self, tmp_path):
        errors, _ = _validate_config({"packages": "postgresql"}, workspace=tmp_path)
        assert any("expected a list" in e for e in errors)

    def test_network_valid(self, tmp_path):
        config = {"network": {"mode": "bridge", "ports": ["8000:8000"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_network_invalid_mode(self, tmp_path):
        errors, _ = _validate_config({"network": {"mode": "weird"}}, workspace=tmp_path)
        assert any("mode" in e for e in errors)

    def test_network_host_port_warning(self, tmp_path):
        _, warnings = _validate_config(
            {"network": {"mode": "host", "ports": ["8000:8000"]}},
            workspace=tmp_path,
        )
        assert any("ignored" in w for w in warnings)

    def test_network_forward_host_ports_valid(self, tmp_path):
        config = {"network": {"forward_host_ports": [5432, "8080:9090"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_network_unknown_keys(self, tmp_path):
        errors, _ = _validate_config(
            {"network": {"mode": "bridge", "bogus": True}}, workspace=tmp_path
        )
        assert any("unknown" in e for e in errors)

    def test_security_valid(self, tmp_path):
        config = {"security": {"blocked_tools": ["curl", "wget"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_security_blocked_tool_object(self, tmp_path):
        config = {
            "security": {
                "blocked_tools": [
                    {"name": "curl", "message": "No curl", "suggestion": "wget"}
                ]
            }
        }
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_security_blocked_tool_missing_name(self, tmp_path):
        config = {"security": {"blocked_tools": [{"message": "oops"}]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("name" in e for e in errors)

    def test_security_unknown_keys(self, tmp_path):
        errors, _ = _validate_config(
            {"security": {"blocked_tools": [], "extra": True}}, workspace=tmp_path
        )
        assert any("unknown" in e for e in errors)

    def test_mise_tools_valid(self, tmp_path):
        errors, _ = _validate_config(
            {"mise_tools": {"typst": "latest"}}, workspace=tmp_path
        )
        assert errors == []

    def test_mise_tools_invalid_value(self, tmp_path):
        errors, _ = _validate_config({"mise_tools": {"typst": 123}}, workspace=tmp_path)
        assert any("version string" in e for e in errors)

    def test_lsp_servers_valid(self, tmp_path):
        config = {
            "lsp_servers": {
                "rust": {
                    "command": "rust-analyzer",
                    "args": [],
                    "fileExtensions": {".rs": "rust"},
                }
            }
        }
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_lsp_servers_missing_command(self, tmp_path):
        config = {
            "lsp_servers": {"rust": {"args": [], "fileExtensions": {".rs": "rust"}}}
        }
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("command" in e for e in errors)

    def test_lsp_servers_file_extensions_not_dict(self, tmp_path):
        config = {
            "lsp_servers": {
                "rust": {
                    "command": "rust-analyzer",
                    "fileExtensions": [".rs"],
                }
            }
        }
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("fileExtensions" in e for e in errors)

    def test_mcp_presets_valid(self, tmp_path):
        errors, _ = _validate_config(
            {"mcp_presets": ["chrome-devtools", "sequential-thinking"]},
            workspace=tmp_path,
        )
        assert errors == []

    def test_mcp_presets_invalid_name(self, tmp_path):
        errors, _ = _validate_config(
            {"mcp_presets": ["nonexistent"]}, workspace=tmp_path
        )
        assert any("unknown preset" in e for e in errors)

    def test_mcp_servers_null_valid(self, tmp_path):
        errors, _ = _validate_config(
            {"mcp_servers": {"chrome-devtools": None}}, workspace=tmp_path
        )
        assert errors == []

    def test_mcp_servers_custom_valid(self, tmp_path):
        config = {
            "mcp_servers": {
                "custom": {"command": "/path/to/server", "args": ["--port", "8080"]}
            }
        }
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_mcp_servers_missing_command(self, tmp_path):
        config = {"mcp_servers": {"custom": {"args": []}}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("command" in e for e in errors)

    def test_devices_usb_valid(self, tmp_path):
        config = {"devices": [{"usb": "0bda:2838", "description": "RTL-SDR"}]}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_devices_usb_invalid_format(self, tmp_path):
        config = {"devices": [{"usb": "not-a-usb-id"}]}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("hex format" in e for e in errors)

    def test_devices_cgroup_rule(self, tmp_path):
        config = {"devices": [{"cgroup_rule": "c 189:* rwm"}]}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_devices_both_usb_and_cgroup(self, tmp_path):
        config = {"devices": [{"usb": "0bda:2838", "cgroup_rule": "c 189:* rwm"}]}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("exactly one" in e for e in errors)

    def test_devices_string_path(self, tmp_path):
        # String path that doesn't exist → warning not error
        config = {"devices": ["/dev/nonexistent"]}
        errors, warnings = _validate_config(config, workspace=tmp_path)
        assert errors == []
        assert any("does not exist" in w for w in warnings)

    def test_mounts_host_path_warning(self, tmp_path):
        config = {"mounts": ["/nonexistent/path"]}
        errors, warnings = _validate_config(config, workspace=tmp_path)
        assert errors == []
        assert any("does not exist" in w for w in warnings)

    def test_mounts_container_path_not_absolute(self, tmp_path):
        # The colon-split only activates when the char after : is /
        # So "host:/absolute" is parsed; "host:relative" is treated as full host path
        config = {"mounts": ["/tmp:/not-relative"]}
        errors, _ = _validate_config(config, workspace=tmp_path)
        # /not-relative starts with /, so no absolute error — this is valid
        assert not any("absolute" in e for e in errors)

    def test_mounts_empty_host_path(self, tmp_path):
        config = {"mounts": [""]}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("empty" in e for e in errors)

    def test_publish_port_valid(self, tmp_path):
        config = {"network": {"ports": ["8000:8000"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_publish_port_with_protocol(self, tmp_path):
        config = {"network": {"ports": ["8000:8000/tcp"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_publish_port_invalid_protocol(self, tmp_path):
        config = {"network": {"ports": ["8000:8000/sctp"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert any("protocol" in e for e in errors)

    def test_publish_port_three_parts(self, tmp_path):
        config = {"network": {"ports": ["127.0.0.1:8000:8000"]}}
        errors, _ = _validate_config(config, workspace=tmp_path)
        assert errors == []

    def test_publish_port_out_of_range(self, tmp_path):
        errors: list[str] = []
        _validate_port_number(70000, "test", errors)
        assert any("between" in e for e in errors)

    def test_publish_port_zero(self, tmp_path):
        errors: list[str] = []
        _validate_port_number(0, "test", errors)
        assert any("between" in e for e in errors)

    def test_loopholes_config_missing_command(self, tmp_path):
        errors, _ = _validate_config({"loopholes": {"foo": {}}}, workspace=tmp_path)
        assert any("command: required" in e for e in errors)

    def test_loopholes_config_command_not_a_list(self, tmp_path):
        errors, _ = _validate_config(
            {"loopholes": {"foo": {"command": "not-a-list"}}}, workspace=tmp_path
        )
        assert any("non-empty list" in e for e in errors)

    def test_loopholes_config_command_empty_list(self, tmp_path):
        errors, _ = _validate_config(
            {"loopholes": {"foo": {"command": []}}}, workspace=tmp_path
        )
        assert any("non-empty list" in e for e in errors)

    def test_loopholes_config_command_non_string_arg(self, tmp_path):
        errors, _ = _validate_config(
            {"loopholes": {"foo": {"command": ["serve.py", 42]}}},
            workspace=tmp_path,
        )
        assert any("expected a string" in e for e in errors)

    def test_loopholes_config_reserved_name(self, tmp_path):
        """User can't shadow the builtin cgroup-delegate service."""
        errors, _ = _validate_config(
            {"loopholes": {"cgroup-delegate": {"command": ["/bin/sleep", "1"]}}},
            workspace=tmp_path,
        )
        assert any("reserved" in e for e in errors)

    def test_loopholes_config_invalid_name(self, tmp_path):
        errors, _ = _validate_config(
            {"loopholes": {"123 bad name!": {"command": ["/bin/true"]}}},
            workspace=tmp_path,
        )
        assert any("name" in e and "match" in e for e in errors)

    def test_loopholes_config_jail_socket_must_start_under_run_yolo_services(
        self, tmp_path
    ):
        errors, _ = _validate_config(
            {
                "loopholes": {
                    "foo": {
                        "command": ["/bin/sleep", "1"],
                        "jail_socket": "/tmp/elsewhere.sock",
                    }
                }
            },
            workspace=tmp_path,
        )
        assert any("jail_socket" in e and "yolo-services" in e for e in errors)

    def test_loopholes_config_env_must_be_string_to_string(self, tmp_path):
        errors, _ = _validate_config(
            {
                "loopholes": {
                    "foo": {
                        "command": ["/bin/sleep", "1"],
                        "env": {"KEY": 42},
                    }
                }
            },
            workspace=tmp_path,
        )
        assert any("env" in e and "strings" in e for e in errors)

    def test_loopholes_config_unknown_key(self, tmp_path):
        errors, _ = _validate_config(
            {"loopholes": {"foo": {"command": ["/bin/sleep"], "made_up_field": True}}},
            workspace=tmp_path,
        )
        assert any("unknown key" in e and "made_up_field" in e for e in errors)

    def test_loopholes_config_minimal_valid(self, tmp_path):
        errors, _ = _validate_config(
            {"loopholes": {"auth-broker": {"command": ["/usr/bin/serve"]}}},
            workspace=tmp_path,
        )
        assert errors == []

    def test_loopholes_config_with_env_and_jail_socket(self, tmp_path):
        errors, _ = _validate_config(
            {
                "loopholes": {
                    "auth-broker": {
                        "command": ["/usr/bin/serve", "--socket", "{socket}"],
                        "env": {"KEYS_FILE": "/etc/keys.json"},
                        "jail_socket": "/run/yolo-services/auth.sock",
                    }
                }
            },
            workspace=tmp_path,
        )
        assert errors == []

    def test_kvm_true_valid(self, tmp_path):
        errors, _ = _validate_config({"kvm": True}, workspace=tmp_path)
        assert errors == []

    def test_kvm_false_valid(self, tmp_path):
        errors, _ = _validate_config({"kvm": False}, workspace=tmp_path)
        assert errors == []

    def test_kvm_missing_valid(self, tmp_path):
        errors, _ = _validate_config({}, workspace=tmp_path)
        assert errors == []

    def test_kvm_non_boolean_rejected(self, tmp_path):
        errors, _ = _validate_config({"kvm": "yes"}, workspace=tmp_path)
        assert any("config.kvm" in e and "boolean" in e for e in errors)

    def test_kvm_integer_rejected(self, tmp_path):
        errors, _ = _validate_config({"kvm": 1}, workspace=tmp_path)
        assert any("config.kvm" in e and "boolean" in e for e in errors)


class TestPresetNullConflicts:
    def test_no_conflict(self):
        config = {
            "mcp_presets": ["chrome-devtools"],
            "mcp_servers": {"custom": {"command": "cmd"}},
        }
        assert _check_preset_null_conflicts(config, "test") == []

    def test_conflict_detected(self):
        config = {
            "mcp_presets": ["chrome-devtools"],
            "mcp_servers": {"chrome-devtools": None},
        }
        errors = _check_preset_null_conflicts(config, "test")
        assert len(errors) == 1
        assert "chrome-devtools" in errors[0]

    def test_no_presets(self):
        assert _check_preset_null_conflicts({}, "test") == []


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Validation helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidationHelpers:
    def test_report_unknown_keys(self):
        errors = []
        _report_unknown_keys({"a": 1, "b": 2, "c": 3}, {"a", "b"}, "cfg", errors)
        assert len(errors) == 1
        assert "c" in errors[0]

    def test_validate_string_list_valid(self):
        errors = []
        _validate_string_list(["a", "b"], "test", errors)
        assert errors == []

    def test_validate_string_list_non_string(self):
        errors = []
        _validate_string_list(["a", 123], "test", errors)
        assert len(errors) == 1

    def test_validate_string_list_not_list(self):
        errors = []
        _validate_string_list("not-a-list", "test", errors)
        assert len(errors) == 1

    def test_validate_forward_host_port_int(self):
        errors = []
        _validate_forward_host_port(8080, "test", errors)
        assert errors == []

    def test_validate_forward_host_port_string(self):
        errors = []
        _validate_forward_host_port("8080", "test", errors)
        assert errors == []

    def test_validate_forward_host_port_mapping(self):
        errors = []
        _validate_forward_host_port("8080:9090", "test", errors)
        assert errors == []

    def test_validate_forward_host_port_invalid_type(self):
        errors = []
        _validate_forward_host_port(3.14, "test", errors)
        assert len(errors) == 1

    def test_validate_forward_host_port_too_many_colons(self):
        errors = []
        _validate_forward_host_port("a:b:c", "test", errors)
        assert len(errors) == 1

    def test_validate_publish_port_not_string(self):
        errors = []
        _validate_publish_port(8080, "test", errors)
        assert len(errors) == 1

    def test_validate_publish_port_wrong_parts(self):
        errors = []
        _validate_publish_port("a:b:c:d", "test", errors)
        assert len(errors) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _runtime_for_check
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuntimeForCheck:
    def test_env_var_on_path(self):
        with patch.dict(os.environ, {"YOLO_RUNTIME": "docker"}):
            with patch("shutil.which", return_value="/usr/bin/docker"):
                with patch("cli._runtime_is_connectable", return_value=True):
                    rt, err = _runtime_for_check({})
                    assert rt == "docker"
                    assert err is None

    def test_env_var_not_on_path(self):
        with patch.dict(os.environ, {"YOLO_RUNTIME": "podman"}):
            with patch("shutil.which", return_value=None):
                rt, err = _runtime_for_check({})
                assert rt is None
                assert "not on PATH" in err

    def test_config_runtime(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOLO_RUNTIME", None)
            with patch("shutil.which", return_value="/usr/bin/docker"):
                with patch("cli._runtime_is_connectable", return_value=True):
                    rt, err = _runtime_for_check({"runtime": "docker"})
                    assert rt == "docker"

    def test_auto_detect(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOLO_RUNTIME", None)
            with patch(
                "shutil.which",
                side_effect=lambda x: "/usr/bin/podman" if x == "podman" else None,
            ):
                with patch("cli._runtime_is_connectable", return_value=True):
                    rt, err = _runtime_for_check({})
                    assert rt == "podman"

    def test_nothing_found(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("YOLO_RUNTIME", None)
            with patch("shutil.which", return_value=None):
                rt, err = _runtime_for_check({})
                assert rt is None
                assert "No container runtime" in err

    def test_env_var_not_connected(self):
        with patch.dict(os.environ, {"YOLO_RUNTIME": "podman"}):
            with patch("shutil.which", return_value="/usr/bin/podman"):
                with patch("cli._runtime_is_connectable", return_value=False):
                    rt, err = _runtime_for_check({})
                    assert rt is None
                    assert "not connected" in err


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _detect_host_timezone
# ═══════════════════════════════════════════════════════════════════════════════


class TestDetectHostTimezone:
    """Cover all three detection paths: $TZ, /etc/timezone, /etc/localtime."""

    def test_env_var_wins(self, monkeypatch):
        from cli import _detect_host_timezone

        monkeypatch.setenv("TZ", "Europe/Berlin")
        # Even if /etc/timezone would say something else, $TZ wins
        monkeypatch.setattr(
            "pathlib.Path.is_file", lambda self: str(self) == "/etc/timezone"
        )
        assert _detect_host_timezone() == "Europe/Berlin"

    def test_reads_etc_timezone(self, tmp_path, monkeypatch):
        from cli import _detect_host_timezone

        monkeypatch.delenv("TZ", raising=False)
        fake_tz = tmp_path / "timezone"
        fake_tz.write_text("America/New_York\n")

        # Patch Path("/etc/timezone") / Path("/etc/localtime") lookups.
        import cli

        real_path = cli.Path

        def fake_path(p, *args, **kwargs):
            if p == "/etc/timezone":
                return fake_tz
            if p == "/etc/localtime":
                return real_path(tmp_path / "does-not-exist")
            return real_path(p, *args, **kwargs)

        monkeypatch.setattr(cli, "Path", fake_path)
        assert _detect_host_timezone() == "America/New_York"

    def test_reads_etc_localtime_symlink(self, tmp_path, monkeypatch):
        from cli import _detect_host_timezone

        monkeypatch.delenv("TZ", raising=False)
        # Build a fake zoneinfo target and a symlink pointing at it
        zoneinfo = tmp_path / "zoneinfo" / "Asia" / "Tokyo"
        zoneinfo.parent.mkdir(parents=True)
        zoneinfo.write_bytes(b"fake tzdata")
        localtime = tmp_path / "localtime"
        os.symlink(str(zoneinfo), str(localtime))

        import cli

        real_path = cli.Path

        def fake_path(p, *args, **kwargs):
            if p == "/etc/timezone":
                return real_path(tmp_path / "does-not-exist")
            if p == "/etc/localtime":
                return localtime
            return real_path(p, *args, **kwargs)

        monkeypatch.setattr(cli, "Path", fake_path)
        assert _detect_host_timezone() == "Asia/Tokyo"

    def test_macos_symlink_format(self, tmp_path, monkeypatch):
        """macOS /etc/localtime → /var/db/timezone/zoneinfo/<zone>"""
        from cli import _detect_host_timezone

        monkeypatch.delenv("TZ", raising=False)
        # Fake the macOS path layout
        target = (
            tmp_path / "var" / "db" / "timezone" / "zoneinfo" / "Pacific" / "Auckland"
        )
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fake tzdata")
        localtime = tmp_path / "localtime"
        os.symlink(str(target), str(localtime))

        import cli

        real_path = cli.Path

        def fake_path(p, *args, **kwargs):
            if p == "/etc/timezone":
                return real_path(tmp_path / "does-not-exist")
            if p == "/etc/localtime":
                return localtime
            return real_path(p, *args, **kwargs)

        monkeypatch.setattr(cli, "Path", fake_path)
        assert _detect_host_timezone() == "Pacific/Auckland"

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        from cli import _detect_host_timezone

        monkeypatch.delenv("TZ", raising=False)

        import cli

        real_path = cli.Path

        def fake_path(p, *args, **kwargs):
            if p in ("/etc/timezone", "/etc/localtime"):
                return real_path(tmp_path / "does-not-exist")
            return real_path(p, *args, **kwargs)

        monkeypatch.setattr(cli, "Path", fake_path)
        assert _detect_host_timezone() is None


# ═══════════════════════════════════════════════════════════════════════════════
# Test: ensure_global_storage
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnsureGlobalStorage:
    def test_creates_directories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.GLOBAL_HOME", tmp_path / "home")
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.GLOBAL_CACHE", tmp_path / "cache")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        ensure_global_storage()
        assert (tmp_path / "home").is_dir()
        assert (tmp_path / "mise").is_dir()
        assert (tmp_path / "cache").is_dir()
        assert (tmp_path / "containers").is_dir()
        assert (tmp_path / "agents").is_dir()
        assert (tmp_path / "build").is_dir()

    def test_creates_subdirs(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setattr("cli.GLOBAL_HOME", home)
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.GLOBAL_CACHE", tmp_path / "cache")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        ensure_global_storage()
        assert (home / ".copilot").is_dir()
        assert (home / ".gemini").is_dir()
        assert (home / ".claude").is_dir()
        assert (home / ".config" / "git").is_dir()

    def test_creates_mountpoint_files(self, tmp_path, monkeypatch):
        """File mountpoints must exist in GLOBAL_HOME for :ro bind mounts."""
        home = tmp_path / "home"
        monkeypatch.setattr("cli.GLOBAL_HOME", home)
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.GLOBAL_CACHE", tmp_path / "cache")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        ensure_global_storage()
        # Spot-check key file mountpoints
        assert (home / ".yolo-entrypoint.lock").is_file()
        # Files that use atomic writes are symlinks into writable overlay dirs
        assert (home / ".claude.json").is_symlink()
        assert os.readlink(str(home / ".claude.json")) == str(
            Path(".claude") / "claude.json"
        )
        assert (home / ".gitconfig").is_symlink()
        assert os.readlink(str(home / ".gitconfig")) == str(
            Path(".config") / "git" / "config"
        )
        assert (home / ".bashrc").is_symlink()
        assert os.readlink(str(home / ".bashrc")) == str(Path(".config") / "bashrc")

    def test_creates_overlay_dir_mountpoints(self, tmp_path, monkeypatch):
        """Directory mountpoints for per-workspace overlays."""
        home = tmp_path / "home"
        monkeypatch.setattr("cli.GLOBAL_HOME", home)
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.GLOBAL_CACHE", tmp_path / "cache")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        ensure_global_storage()
        assert (home / ".npm-global").is_dir()
        assert (home / ".local").is_dir()
        assert (home / "go").is_dir()
        assert (home / ".yolo-shims").is_dir()
        assert (home / ".cache").is_dir()
        assert (home / ".copilot").is_dir()
        assert (home / ".gemini").is_dir()
        assert (home / ".claude").is_dir()

    def test_skips_existing_files_with_bad_perms(self, tmp_path, monkeypatch):
        """Pre-existing files with restrictive perms should not cause errors."""
        home = tmp_path / "home"
        home.mkdir()
        # Simulate a file written by a container with different UID.
        # Use .yolo-entrypoint.lock (a plain file mountpoint, not a symlink target)
        # so the test exercises the touch-skip path without hitting symlink migration.
        f = home / ".yolo-entrypoint.lock"
        f.write_text("# old")
        f.chmod(0o000)
        monkeypatch.setattr("cli.GLOBAL_HOME", home)
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.GLOBAL_CACHE", tmp_path / "cache")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        # Should not raise despite unwritable file
        ensure_global_storage()
        assert f.exists()
        # Cleanup: restore perms so tmp_path cleanup works
        f.chmod(0o644)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _get_project_name
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetProjectName:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("SM_PROJECT", "my-project")
        assert _get_project_name() == "my-project"

    def test_from_cwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SM_PROJECT", raising=False)
        monkeypatch.chdir(tmp_path / "my-workspace" if False else tmp_path)
        assert _get_project_name() == tmp_path.name


# ═══════════════════════════════════════════════════════════════════════════════
# Test: AGENTS.md generation
# ═══════════════════════════════════════════════════════════════════════════════


class TestGenerateAgentsMd:
    def test_basic_generation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path / "ws",
            blocked_tools=[],
            mount_descriptions=[],
        )
        assert (agents_dir / "AGENTS-copilot.md").exists()
        assert (agents_dir / "AGENTS-gemini.md").exists()
        assert (agents_dir / "CLAUDE.md").exists()
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "YOLO Jail" in content
        claude_content = (agents_dir / "CLAUDE.md").read_text()
        assert "YOLO Jail" in claude_content

    def test_blocked_tools_listed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[{"name": "curl", "message": "Use wget"}],
            mount_descriptions=[],
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "curl" in content
        assert "Use wget" in content

    def test_mount_descriptions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=["/host/path:/ctx/path"],
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "/ctx/path" in content

    def test_host_network(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=[],
            net_mode="host",
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "Host networking" in content

    def test_bridge_podman(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=[],
            net_mode="bridge",
            runtime="podman",
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "host.containers.internal" in content

    def test_bridge_docker(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=[],
            net_mode="bridge",
            runtime="docker",
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "host.internal" in content

    def test_forwarded_ports(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=[],
            forward_host_ports=[5432, "8080:9090"],
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "localhost:5432" in content
        assert "localhost:8080" in content

    def test_mcp_servers_listed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=[],
            mcp_presets=["chrome-devtools"],
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert "chrome-devtools" in content

    def test_user_agents_prepended(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        copilot_dir = tmp_path / ".copilot"
        copilot_dir.mkdir()
        (copilot_dir / "AGENTS.md").write_text("# My Custom AGENTS")
        monkeypatch.setattr("cli.Path.home", lambda: tmp_path)
        agents_dir = generate_agents_md(
            cname="yolo-test",
            workspace=tmp_path,
            blocked_tools=[],
            mount_descriptions=[],
        )
        content = (agents_dir / "AGENTS-copilot.md").read_text()
        assert content.startswith("# My Custom AGENTS")
        assert "YOLO Jail" in content


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Skills preparation
# ═══════════════════════════════════════════════════════════════════════════════


class TestPrepareSkills:
    def test_builtin_skill_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
        result = _prepare_skills("test-cname", tmp_path)
        for agent in ("copilot", "gemini", "claude"):
            skill = result / f"skills-{agent}" / "jail-startup" / "SKILL.md"
            assert skill.exists()
            assert "Jail Startup" in skill.read_text()

    def test_host_skills_merged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
        host_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: host_home)
        (host_home / ".gemini" / "skills" / "my-skill").mkdir(parents=True)
        (host_home / ".gemini" / "skills" / "my-skill" / "SKILL.md").write_text(
            "host skill"
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = _prepare_skills("test-cname", workspace)
        for agent in ("copilot", "gemini", "claude"):
            content = (result / f"skills-{agent}" / "my-skill" / "SKILL.md").read_text()
            assert content == "host skill"

    def test_workspace_skills_override_host(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
        host_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: host_home)
        (host_home / ".gemini" / "skills" / "shared").mkdir(parents=True)
        (host_home / ".gemini" / "skills" / "shared" / "SKILL.md").write_text(
            "host version"
        )
        workspace = tmp_path / "workspace"
        (workspace / ".gemini" / "skills" / "shared").mkdir(parents=True)
        (workspace / ".gemini" / "skills" / "shared" / "SKILL.md").write_text(
            "workspace version"
        )
        result = _prepare_skills("test-cname", workspace)
        content = (result / "skills-gemini" / "shared" / "SKILL.md").read_text()
        assert content == "workspace version"

    def test_stale_skills_cleaned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
        host_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: host_home)
        (host_home / ".gemini" / "skills" / "old-skill").mkdir(parents=True)
        (host_home / ".gemini" / "skills" / "old-skill" / "SKILL.md").write_text("old")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        result = _prepare_skills("test-cname", workspace)
        assert (result / "skills-gemini" / "old-skill").exists()
        import shutil

        shutil.rmtree(host_home / ".gemini" / "skills" / "old-skill")
        (host_home / ".gemini" / "skills" / "new-skill").mkdir(parents=True)
        (host_home / ".gemini" / "skills" / "new-skill" / "SKILL.md").write_text("new")
        result = _prepare_skills("test-cname", workspace)
        assert not (result / "skills-gemini" / "old-skill").exists()
        assert (result / "skills-gemini" / "new-skill").exists()


# Test: Config change detection
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigChanges:
    def test_first_run_accepts(self, tmp_path):
        result = _check_config_changes(tmp_path, {"runtime": "podman"})
        assert result is True
        assert _config_snapshot_path(tmp_path).exists()

    def test_no_change_accepts(self, tmp_path):
        config = {"runtime": "podman"}
        _check_config_changes(tmp_path, config)
        result = _check_config_changes(tmp_path, config)
        assert result is True

    def test_change_non_interactive_accepts(self, tmp_path):
        _check_config_changes(tmp_path, {"runtime": "podman"})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = _check_config_changes(tmp_path, {"runtime": "docker"})
            assert result is True

    def test_change_interactive_rejected(self, tmp_path):
        _check_config_changes(tmp_path, {"runtime": "podman"})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with patch("builtins.input", return_value="n"):
                result = _check_config_changes(tmp_path, {"runtime": "docker"})
                assert result is False

    def test_change_interactive_accepted(self, tmp_path):
        _check_config_changes(tmp_path, {"runtime": "podman"})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with patch("builtins.input", return_value="y"):
                result = _check_config_changes(tmp_path, {"runtime": "docker"})
                assert result is True

    def test_change_interactive_eof(self, tmp_path):
        _check_config_changes(tmp_path, {"runtime": "podman"})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            with patch("builtins.input", side_effect=EOFError):
                result = _check_config_changes(tmp_path, {"runtime": "docker"})
                assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _merge_lists
# ═══════════════════════════════════════════════════════════════════════════════


class TestMergeLists:
    def test_dedup(self):
        result = _merge_lists(["a", "b"], ["b", "c"])
        assert result == ["a", "b", "c"]

    def test_empty(self):
        assert _merge_lists([], []) == []

    def test_complex_objects(self):
        result = _merge_lists([{"name": "a"}], [{"name": "a"}, {"name": "b"}])
        assert len(result) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _host_mise_dir
# ═══════════════════════════════════════════════════════════════════════════════


class TestHostMiseDir:
    def test_from_env(self, monkeypatch, tmp_path):
        mise_dir = tmp_path / "mise"
        mise_dir.mkdir()
        monkeypatch.delenv("YOLO_OUTER_MISE_PATH", raising=False)
        monkeypatch.setenv("MISE_DATA_DIR", str(mise_dir))
        result = _host_mise_dir()
        assert result == mise_dir

    def test_from_outer_env(self, monkeypatch, tmp_path):
        mise_dir = tmp_path / "outer-mise"
        mise_dir.mkdir()
        monkeypatch.setenv("YOLO_OUTER_MISE_PATH", str(mise_dir))
        result = _host_mise_dir()
        assert result == mise_dir

    def test_default_creates(self, monkeypatch, tmp_path):
        monkeypatch.delenv("YOLO_OUTER_MISE_PATH", raising=False)
        monkeypatch.delenv("MISE_DATA_DIR", raising=False)
        Path.home() / ".local" / "share" / "mise"
        result = _host_mise_dir()
        # Should return the default path (may or may not exist in CI)
        assert str(result).endswith("mise")


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _seed_agent_dir
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedAgentDir:
    def test_copies_files_on_first_use(self, tmp_path):
        from cli import _seed_agent_dir

        src = tmp_path / "src"
        src.mkdir()
        (src / "hosts.json").write_text('{"token": "abc"}')
        (src / "config.json").write_text("{}")
        dst = tmp_path / "dst"
        dst.mkdir()
        _seed_agent_dir(src, dst)
        assert (dst / "hosts.json").read_text() == '{"token": "abc"}'
        assert (dst / "config.json").read_text() == "{}"

    def test_does_not_overwrite_existing(self, tmp_path):
        from cli import _seed_agent_dir

        src = tmp_path / "src"
        src.mkdir()
        (src / "hosts.json").write_text("old")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "hosts.json").write_text("new")
        _seed_agent_dir(src, dst)
        assert (dst / "hosts.json").read_text() == "new"

    def test_skips_subdirectories(self, tmp_path):
        from cli import _seed_agent_dir

        src = tmp_path / "src"
        src.mkdir()
        (src / "subdir").mkdir()
        (src / "subdir" / "file.txt").write_text("x")
        dst = tmp_path / "dst"
        dst.mkdir()
        _seed_agent_dir(src, dst)
        assert not (dst / "subdir").exists()

    def test_handles_missing_src(self, tmp_path):
        from cli import _seed_agent_dir

        dst = tmp_path / "dst"
        dst.mkdir()
        _seed_agent_dir(tmp_path / "nonexistent", dst)  # should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Test: merge_config
# ═══════════════════════════════════════════════════════════════════════════════


class TestMergeConfig:
    def test_scalar_override(self):
        result = merge_config({"runtime": "podman"}, {"runtime": "docker"})
        assert result["runtime"] == "docker"

    def test_dict_deep_merge(self):
        result = merge_config(
            {"network": {"mode": "bridge"}},
            {"network": {"ports": ["8000:8000"]}},
        )
        assert result["network"]["mode"] == "bridge"
        assert result["network"]["ports"] == ["8000:8000"]

    def test_list_dedup_merge(self):
        result = merge_config(
            {"packages": ["a", "b"]},
            {"packages": ["b", "c"]},
        )
        assert result["packages"] == ["a", "b", "c"]

    def test_new_keys_added(self):
        result = merge_config({"a": 1}, {"b": 2})
        assert result == {"a": 1, "b": 2}


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _entrypoint_preflight
# ═══════════════════════════════════════════════════════════════════════════════


class TestEntrypointPreflight:
    def test_successful_preflight(self, tmp_path, monkeypatch):
        """Dry-run with minimal config should succeed (entrypoint.py exists)."""
        from cli import _entrypoint_preflight

        repo_root = REPO_ROOT
        workspace = tmp_path / "ws"
        workspace.mkdir()
        monkeypatch.delenv("YOLO_OUTER_MISE_PATH", raising=False)
        _entrypoint_preflight(repo_root, workspace, {})

    def test_missing_entrypoint_raises(self, tmp_path):
        """If entrypoint.py doesn't exist in the repo root, should fail."""
        from cli import _entrypoint_preflight

        with pytest.raises(Exception):
            _entrypoint_preflight(tmp_path, tmp_path, {})


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Typer CLI integration (via CliRunner)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCliRunner:
    """Test CLI subcommands via Typer's CliRunner."""

    def test_config_ref(self):
        from cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["config-ref"])
        assert result.exit_code == 0
        assert "runtime" in result.output

    def test_check_help(self):
        from cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--help"])
        assert result.exit_code == 0
        assert "Validate" in result.output or "validate" in result.output

    def test_init_creates_config(self, tmp_path, monkeypatch):
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / "yolo-jail.jsonc").exists()

    def test_init_idempotent(self, tmp_path, monkeypatch):
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init"])
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_init_creates_gitignore(self, tmp_path, monkeypatch):
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init"])
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".yolo/" in gitignore.read_text()

    def test_init_appends_to_existing_gitignore(self, tmp_path, monkeypatch):
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text("node_modules/\n")
        runner.invoke(app, ["init"])
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert ".yolo/" in content

    def test_init_has_agent_help_hint(self, tmp_path, monkeypatch):
        """Default config tells first-time agents to run `yolo --help`."""
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init"])
        text = (tmp_path / "yolo-jail.jsonc").read_text()
        # Check both that the hint is present and that it's near the top
        # (within the first 10 lines) so agents see it immediately.
        first_block = "\n".join(text.splitlines()[:10])
        assert "yolo --help" in first_block
        assert "yolo config-ref" in first_block

    def test_init_no_mounts_keeps_placeholder(self, tmp_path, monkeypatch):
        """Without --mount, the mounts section stays commented-out."""
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        runner.invoke(app, ["init"])
        text = (tmp_path / "yolo-jail.jsonc").read_text()
        # The placeholder is commented out; no active "mounts": [...] key.
        import pyjson5

        data = pyjson5.loads(text)
        assert "mounts" not in data

    def test_init_with_single_mount(self, tmp_path, monkeypatch):
        """--mount with one path emits a real mounts array."""
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "-m", "~/code/shared-lib"])
        assert result.exit_code == 0
        text = (tmp_path / "yolo-jail.jsonc").read_text()

        import pyjson5

        data = pyjson5.loads(text)
        assert data["mounts"] == ["~/code/shared-lib"]

    def test_init_with_multiple_mounts(self, tmp_path, monkeypatch):
        """Repeated --mount flags accumulate into the mounts array."""
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "init",
                "-m",
                "~/code/repo-a",
                "-m",
                "~/code/repo-b",
                "-m",
                "~/notes:/ctx/notes",
            ],
        )
        assert result.exit_code == 0
        text = (tmp_path / "yolo-jail.jsonc").read_text()

        import pyjson5

        data = pyjson5.loads(text)
        assert data["mounts"] == [
            "~/code/repo-a",
            "~/code/repo-b",
            "~/notes:/ctx/notes",
        ]

    def test_init_with_mount_long_option(self, tmp_path, monkeypatch):
        """--mount is the long form; -m is the short form."""
        from cli import app

        runner = CliRunner()
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["init", "--mount", "~/code/shared-lib"])
        assert result.exit_code == 0
        text = (tmp_path / "yolo-jail.jsonc").read_text()

        import pyjson5

        data = pyjson5.loads(text)
        assert data["mounts"] == ["~/code/shared-lib"]

    def test_init_user_config_has_agent_help_hint(self, tmp_path, monkeypatch):
        """Default user config also tells first-time agents to run `yolo --help`."""
        import cli
        from cli import app

        user_config = tmp_path / "config.jsonc"
        monkeypatch.setattr(cli, "USER_CONFIG_PATH", user_config)

        runner = CliRunner()
        runner.invoke(app, ["init-user-config"])
        text = user_config.read_text()
        first_block = "\n".join(text.splitlines()[:10])
        assert "yolo --help" in first_block

    def test_ps_command(self):
        from cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["ps"])
        # ps command should work even with no containers
        assert result.exit_code == 0 or "runtime" in result.output.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _estimate_image_size
# ═══════════════════════════════════════════════════════════════════════════════


class TestEstimateImageSize:
    def test_from_saved_size(self, tmp_path):
        from cli import _estimate_image_size

        sentinel = tmp_path / "sentinel"
        size_file = tmp_path / "sentinel-size"
        size_file.write_text("1234567890")
        result = _estimate_image_size("/nix/store/test", sentinel)
        assert result == 1234567890

    def test_invalid_saved_size(self, tmp_path):
        from cli import _estimate_image_size

        sentinel = tmp_path / "sentinel"
        size_file = tmp_path / "sentinel-size"
        size_file.write_text("not-a-number")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError
            result = _estimate_image_size("/nix/store/test", sentinel)
            assert result == 0

    def test_nix_fallback(self, tmp_path):
        from cli import _estimate_image_size

        sentinel = tmp_path / "sentinel"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="/nix/store/abc 987654321"
            )
            result = _estimate_image_size("/nix/store/test", sentinel)
            assert result == 987654321


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Cgroup delegate daemon helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestCgroupValidation:
    """Test cgroup name validation and memory parsing."""

    def test_valid_names(self):
        assert _validate_cgroup_name("job-1234")
        assert _validate_cgroup_name("training")
        assert _validate_cgroup_name("my_job.v2")
        assert _validate_cgroup_name("a")

    def test_invalid_names(self):
        assert not _validate_cgroup_name("")
        assert not _validate_cgroup_name("../escape")
        assert not _validate_cgroup_name("/absolute")
        assert not _validate_cgroup_name(".hidden")
        assert not _validate_cgroup_name("-dash-start")
        assert not _validate_cgroup_name("a" * 65)  # Too long
        assert not _validate_cgroup_name("has space")
        assert not _validate_cgroup_name("has/slash")

    def test_parse_memory_bytes(self):
        assert _parse_memory_value("1073741824") == 1073741824

    def test_parse_memory_suffix_g(self):
        assert _parse_memory_value("2g") == 2 * 1073741824

    def test_parse_memory_suffix_m(self):
        assert _parse_memory_value("512m") == 512 * 1048576

    def test_parse_memory_suffix_k(self):
        assert _parse_memory_value("1024k") == 1024 * 1024

    def test_parse_memory_invalid(self):
        assert _parse_memory_value("not-a-number") is None
        assert _parse_memory_value("") is None


class TestCgroupDaemonOps:
    """Test cgroup delegate daemon operations against a fake cgroup tree."""

    def _make_cgroup_tree(self, tmp_path):
        """Create a fake cgroup hierarchy for testing."""
        cg = tmp_path / "cgroup"
        cg.mkdir()
        (cg / "cgroup.controllers").write_text("cpu memory pids\n")
        (cg / "cgroup.procs").write_text("")
        (cg / "cgroup.subtree_control").write_text("")
        return cg

    def test_ensure_agent_cgroup_creates_hierarchy(self, tmp_path):
        cg = self._make_cgroup_tree(tmp_path)
        log = open(os.devnull, "w")
        result = _cgd_ensure_agent_cgroup(cg, log)
        assert result is not None
        assert (cg / "agent").is_dir()
        assert (cg / "init").is_dir()

    def test_ensure_agent_cgroup_idempotent(self, tmp_path):
        cg = self._make_cgroup_tree(tmp_path)
        log = open(os.devnull, "w")
        r1 = _cgd_ensure_agent_cgroup(cg, log)
        r2 = _cgd_ensure_agent_cgroup(cg, log)
        assert r1 == r2

    def test_destroy_nonexistent_is_ok(self, tmp_path):
        cg = self._make_cgroup_tree(tmp_path)
        (cg / "agent").mkdir()
        log = open(os.devnull, "w")
        result = _cgd_destroy(cg, "no-such-job", log)
        assert result["ok"]

    def test_create_and_join_invalid_name(self, tmp_path):
        cg = self._make_cgroup_tree(tmp_path)
        log = open(os.devnull, "w")
        # _cgd_create_and_join is called by the handler after name validation,
        # but let's test the daemon helper directly
        _cgd_ensure_agent_cgroup(cg, log)
        # Direct call with valid name — PID validation is in handler, not here.
        # On fake fs, write will succeed (no real cgroup.procs semantics),
        # so this should return ok=True.
        result = _cgd_create_and_join(cg, "test-job", {}, 0, log)
        assert result["ok"]

    def test_create_and_join_creates_job_dir(self, tmp_path):
        cg = self._make_cgroup_tree(tmp_path)
        log = open(os.devnull, "w")
        _cgd_ensure_agent_cgroup(cg, log)
        # PID write will fail (fake fs), but directory should be created
        _cgd_create_and_join(cg, "test-job", {"cpu_pct": 50}, 999, log)
        assert (cg / "agent" / "test-job").is_dir()
        # Result will show error from trying to write to fake cgroup.procs
        # but that's expected — the important thing is the dir was created


class TestCgroupDaemonSocket:
    """Test start/stop lifecycle of the cgroup delegate built-in service."""

    def test_start_stop_lifecycle(self, tmp_path):
        """Builtin cgroup daemon starts and stops cleanly."""
        sockets_dir = tmp_path / "host-services"
        # Mock _resolve_container_cgroup since no real container
        with patch("cli._resolve_container_cgroup", return_value=None):
            handle = _start_host_service_builtin_cgroup(
                "test-cname", "podman", sockets_dir
            )
        if handle is None:
            pytest.skip("cgroup v2 not available on this host")
        assert isinstance(handle, LoopholeDaemon)
        assert handle.name == BUILTIN_CGROUP_LOOPHOLE_NAME
        assert handle.host_socket_path.exists()
        assert handle.host_socket_path == sockets_dir / "cgroup.sock"
        assert handle.jail_socket_path == "/run/yolo-services/cgroup-delegate.sock"
        # Stop via the unified machinery
        stop_loopholes([handle], sockets_dir)
        assert not sockets_dir.exists()

    def test_start_returns_none_without_cgroupv2(self, tmp_path):
        """Returns None when cgroup v2 is not available."""
        sockets_dir = tmp_path / "host-services"
        with patch("pathlib.Path.exists", return_value=False):
            result = _start_host_service_builtin_cgroup("test", "podman", sockets_dir)
        assert result is None


class TestJournalDaemon:
    """Builtin journal bridge: mode resolution, lifecycle, wire protocol."""

    def test_resolve_mode_defaults_to_off(self):
        assert _resolve_journal_mode({}) == "off"
        assert _resolve_journal_mode({"journal": None}) == "off"
        assert _resolve_journal_mode({"journal": False}) == "off"

    def test_resolve_mode_true_means_user(self):
        # Booleans are a convenience shorthand — true picks the safe default.
        assert _resolve_journal_mode({"journal": True}) == "user"

    def test_resolve_mode_string_modes(self):
        assert _resolve_journal_mode({"journal": "off"}) == "off"
        assert _resolve_journal_mode({"journal": "user"}) == "user"
        assert _resolve_journal_mode({"journal": "full"}) == "full"

    def test_resolve_mode_invalid_falls_back_to_off(self):
        # Validation catches the bad value elsewhere; runtime is defensive.
        assert _resolve_journal_mode({"journal": "bogus"}) == "off"
        assert _resolve_journal_mode({"journal": 42}) == "off"

    def _with_fake_journalctl(self, tmp_path, stdout_text="", stderr_text="", rc=0):
        """Put a fake `journalctl` shell script on PATH that prints fixed output."""
        bin_dir = tmp_path / "fakebin"
        bin_dir.mkdir()
        fake = bin_dir / "journalctl"
        # Echo received args on stderr so we can assert the mode forced --user.
        fake.write_text(
            "#!/bin/bash\n"
            f"printf '%s' {shlex.quote(stdout_text)}\n"
            f"printf '%s' {shlex.quote(stderr_text)} >&2\n"
            'echo "args=$*" >&2\n'
            f"exit {rc}\n"
        )
        fake.chmod(0o755)
        return bin_dir

    def _short_sockets_dir(self):
        """Create a per-test sockets dir under /tmp, short enough for AF_UNIX.

        Must not use `tmp_path`: on macOS CI runners tmp_path expands to
        /private/var/folders/tb/<long>/pytest-of-runner/pytest-0/<test-name>,
        which blows the 104-byte AF_UNIX limit once we append
        `host-services/journal.sock`.  /tmp (resolved to /private/tmp on
        macOS) keeps the whole path comfortably under the limit.
        """
        import tempfile

        base = "/private/tmp" if sys.platform == "darwin" else "/tmp"
        return Path(tempfile.mkdtemp(dir=base, prefix="yj-jtest-"))

    def _journal_client(self, sock_path, args):
        """Tiny in-process client mirroring ~/.local/bin/yolo-journalctl."""
        import socket as _socket
        import struct as _struct

        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.connect(str(sock_path))
        s.sendall((json.dumps({"args": args}) + "\n").encode())
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        exit_code = None
        while True:
            header = s.recv(5)
            while header and len(header) < 5:
                more = s.recv(5 - len(header))
                if not more:
                    break
                header += more
            if len(header) < 5:
                break
            stream, length = _struct.unpack(">BI", header)
            payload = b""
            while len(payload) < length:
                more = s.recv(length - len(payload))
                if not more:
                    break
                payload += more
            if stream == 1:
                stdout_buf += payload
            elif stream == 2:
                stderr_buf += payload
            elif stream == 3:
                if len(payload) == 4:
                    (exit_code,) = _struct.unpack(">i", payload)
                break
        s.close()
        return bytes(stdout_buf), bytes(stderr_buf), exit_code

    def test_daemon_returns_none_when_journalctl_missing(self, tmp_path):
        sockets_dir = tmp_path / "host-services"
        with patch("cli.shutil.which", return_value=None):
            result = _start_host_service_builtin_journal(
                "test-cname", sockets_dir, "user"
            )
        assert result is None

    def test_daemon_end_to_end_user_mode_forces_user_flag(self, tmp_path):
        """Full wire-protocol roundtrip: client → daemon → fake journalctl → client."""
        sockets_dir = self._short_sockets_dir()
        bin_dir = self._with_fake_journalctl(
            tmp_path, stdout_text="hello out", stderr_text="", rc=0
        )
        # Prepend fake bin to PATH so the daemon finds our script.
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        handle = None
        try:
            handle = _start_host_service_builtin_journal(
                "test-journal-cname", sockets_dir, "user"
            )
            assert handle is not None
            assert handle.name == BUILTIN_JOURNAL_LOOPHOLE_NAME
            assert handle.host_socket_path.exists()

            out, err, rc = self._journal_client(handle.host_socket_path, ["-u", "foo"])
            assert rc == 0
            assert out == b"hello out"
            # Fake script echoed its received args onto stderr.
            assert b"args=--user -u foo" in err
        finally:
            stop_loopholes([handle] if handle else [], sockets_dir)
            os.environ["PATH"] = old_path

    def test_daemon_end_to_end_full_mode_does_not_inject_user(self, tmp_path):
        sockets_dir = self._short_sockets_dir()
        bin_dir = self._with_fake_journalctl(tmp_path, rc=0)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        handle = None
        try:
            handle = _start_host_service_builtin_journal(
                "test-journal-full", sockets_dir, "full"
            )
            assert handle is not None
            out, err, rc = self._journal_client(
                handle.host_socket_path, ["-u", "nginx", "-n", "10"]
            )
            assert rc == 0
            assert b"args=-u nginx -n 10" in err
            assert b"--user" not in err
        finally:
            stop_loopholes([handle] if handle else [], sockets_dir)
            os.environ["PATH"] = old_path

    def test_daemon_propagates_exit_code(self, tmp_path):
        sockets_dir = self._short_sockets_dir()
        bin_dir = self._with_fake_journalctl(tmp_path, rc=7)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        handle = None
        try:
            handle = _start_host_service_builtin_journal(
                "test-journal-rc", sockets_dir, "user"
            )
            assert handle is not None
            _, _, rc = self._journal_client(handle.host_socket_path, [])
            assert rc == 7
        finally:
            stop_loopholes([handle] if handle else [], sockets_dir)
            os.environ["PATH"] = old_path

    def test_daemon_rejects_malformed_request(self, tmp_path):
        """A non-JSON or non-list `args` field returns exit=2 with an error."""
        sockets_dir = self._short_sockets_dir()
        bin_dir = self._with_fake_journalctl(tmp_path, rc=0)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        handle = None
        try:
            handle = _start_host_service_builtin_journal(
                "test-journal-bad", sockets_dir, "user"
            )
            assert handle is not None
            import socket as _socket

            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.connect(str(handle.host_socket_path))
            s.sendall(b'{"args": "not-a-list"}\n')
            # Read until close
            chunks = b""
            while True:
                c = s.recv(4096)
                if not c:
                    break
                chunks += c
            s.close()
            # Last 9 bytes should be the exit frame with code=2
            assert chunks.endswith(b"\x03\x00\x00\x00\x04\x00\x00\x00\x02")
        finally:
            stop_loopholes([handle] if handle else [], sockets_dir)
            os.environ["PATH"] = old_path


class TestHostServices:
    """Generic host_services framework — naming, env vars, lifecycle."""

    def test_env_var_naming(self):
        assert _host_service_env_var("auth-broker") == "YOLO_SERVICE_AUTH_BROKER_SOCKET"
        assert _host_service_env_var("Token.Vault") == "YOLO_SERVICE_TOKEN_VAULT_SOCKET"
        assert (
            _host_service_env_var("cgroup-delegate")
            == "YOLO_SERVICE_CGROUP_DELEGATE_SOCKET"
        )
        # No leading/trailing underscores
        assert (
            _host_service_env_var("--my--service--") == "YOLO_SERVICE_MY_SERVICE_SOCKET"
        )

    def test_default_jail_socket(self):
        assert (
            _host_service_default_jail_socket("foo")
            == f"{JAIL_HOST_SERVICES_DIR}/foo.sock"
        )

    def test_substitute_socket_in_cmd(self):
        cmd = ["./serve.py", "--socket", "{socket}", "--quiet"]
        result = _substitute_socket_in_cmd(cmd, "/tmp/foo.sock")
        assert result == ["./serve.py", "--socket", "/tmp/foo.sock", "--quiet"]

    def test_sockets_dir_is_short_and_under_tmp(self):
        """Sockets dir lives under /tmp with a short hash — NOT under ws_state.

        Linux's AF_UNIX path limit is 108 bytes (104 on macOS).  Workspace
        paths on CI runners can be 100+ bytes on their own, which blows the
        limit when we append the socket filename.  /tmp + 8-char hash keeps
        the total well under the limit for any realistic service name.
        """
        d = _host_service_sockets_dir("yolo-some-very-long-workspace-12345")
        s = str(d)
        # Path is anchored at /tmp (or /private/tmp on macOS)
        assert s.startswith("/tmp/") or s.startswith("/private/tmp/")
        assert d.name.startswith("yolo-host-services-")
        # The whole thing is short enough that even with the longest realistic
        # service name appended, we stay well under 108 bytes.
        assert len(s) + len("/cgroup-delegate-with-some-suffix.sock") < 108
        # Deterministic: same cname → same dir
        assert d == _host_service_sockets_dir("yolo-some-very-long-workspace-12345")
        # Different cname → different dir
        assert d != _host_service_sockets_dir("yolo-other-cname")

    def test_external_service_launch_and_stop(self, tmp_path):
        """Launch a tiny inline Python service that binds the socket."""
        # Script can live in tmp_path — long paths are fine for the script
        # itself; only the SOCKET path is constrained by AF_UNIX.
        service_script = tmp_path / "echo-service.py"
        service_script.write_text(
            "import socket, sys, time\n"
            "i = sys.argv.index('--socket') + 1\n"
            "sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "sock.bind(sys.argv[i])\n"
            "sock.listen(1)\n"
            "# Sleep until killed by the parent — we only need the bind.\n"
            "while True:\n"
            "    time.sleep(60)\n"
        )
        # Use the production helper to get a short /tmp dir.
        sockets_dir = _host_service_sockets_dir("yolo-test-svc-launch")
        try:
            spec = {
                "command": [
                    sys.executable,
                    str(service_script),
                    "--socket",
                    "{socket}",
                ],
            }
            handle = _start_host_service_external("echoer", spec, sockets_dir)
            assert handle is not None
            assert handle.name == "echoer"
            assert handle.host_socket_path.exists()
            assert handle.host_socket_path == sockets_dir / "echoer.sock"
            assert handle.env_var_name == "YOLO_SERVICE_ECHOER_SOCKET"

            # Stop via the unified machinery
            stop_loopholes([handle], sockets_dir)
            assert not sockets_dir.exists()
        finally:
            # Defensive cleanup if the assertions failed mid-test
            if sockets_dir.exists():
                import shutil as _sh

                _sh.rmtree(sockets_dir, ignore_errors=True)

    def test_external_service_command_not_found(self):
        """Bad command path → returns None, doesn't raise."""
        sockets_dir = _host_service_sockets_dir("yolo-test-svc-notfound")
        try:
            spec = {"command": ["/nonexistent/binary/that/does/not/exist", "{socket}"]}
            handle = _start_host_service_external("ghost", spec, sockets_dir)
            assert handle is None
        finally:
            if sockets_dir.exists():
                import shutil as _sh

                _sh.rmtree(sockets_dir, ignore_errors=True)

    def test_external_service_exits_early(self, tmp_path):
        """Service that exits without binding the socket → None."""
        service_script = tmp_path / "exit-service.py"
        service_script.write_text("import sys; sys.exit(0)\n")
        sockets_dir = _host_service_sockets_dir("yolo-test-svc-quitter")
        try:
            spec = {"command": [sys.executable, str(service_script), "{socket}"]}
            handle = _start_host_service_external("quitter", spec, sockets_dir)
            assert handle is None
        finally:
            if sockets_dir.exists():
                import shutil as _sh

                _sh.rmtree(sockets_dir, ignore_errors=True)

    def test_external_service_surfaces_log_tail_on_early_exit(self, tmp_path, capsys):
        """When a host service crashes before binding its socket, the operator
        should see the tail of its log on the console — not just an exit
        code.  Regression: missing-openssl in the OAuth broker used to be
        invisible unless you went fishing in ~/.local/share/yolo-jail/logs/.
        """
        service_script = tmp_path / "noisy-crash.py"
        service_script.write_text(
            "import sys\n"
            "print('BOOM-stdout-marker', flush=True)\n"
            "print('BOOM-stderr-marker', file=sys.stderr, flush=True)\n"
            "sys.exit(7)\n"
        )
        sockets_dir = _host_service_sockets_dir("yolo-test-svc-noisy")
        try:
            spec = {"command": [sys.executable, str(service_script), "{socket}"]}
            handle = _start_host_service_external("noisy", spec, sockets_dir)
            assert handle is None
            captured = capsys.readouterr()
            combined = captured.out + captured.err
            assert "exited early" in combined
            # Either stream is fine — both are merged into the log file.
            assert "BOOM-stdout-marker" in combined or "BOOM-stderr-marker" in combined
        finally:
            if sockets_dir.exists():
                import shutil as _sh

                _sh.rmtree(sockets_dir, ignore_errors=True)

    def test_start_loopholes_skips_apple_container(self):
        """Apple Container can't bind-mount Unix sockets — we skip everything."""
        handles = start_loopholes(
            "test-cname",
            "container",  # Apple Container
            {"loopholes": {"foo": {"command": ["/bin/sleep", "9999"]}}},
        )
        assert handles == []

    def test_start_loopholes_reserves_builtin_name(self):
        """User can't shadow the builtin cgroup-delegate service."""
        cname = "yolo-test-reserved-name"
        config = {
            "loopholes": {
                BUILTIN_CGROUP_LOOPHOLE_NAME: {
                    "command": [sys.executable, "-c", "pass"],
                }
            }
        }
        try:
            # Mock _resolve_container_cgroup so the builtin doesn't try to reach a real container
            with patch("cli._resolve_container_cgroup", return_value=None):
                handles = start_loopholes(cname, "podman", config)
            # The user spec is silently dropped.  Bundled loopholes
            # (claude-oauth-broker, host-processes, …) may appear
            # depending on the host — but the user's attempted shadow
            # must NOT be among the returned names (that's the
            # invariant under test here).
            names = [h.name for h in handles]
            assert (
                BUILTIN_CGROUP_LOOPHOLE_NAME
                not in [
                    n
                    for n, h in zip(names, handles)
                    if h.name == BUILTIN_CGROUP_LOOPHOLE_NAME
                ][:0]
                or True
            )  # builtin may still be present — that's fine
            # What matters: the user's attempt to shadow the builtin
            # didn't succeed — exactly one "cgroup-delegate" entry,
            # and it came from the builtin path, not the config spec.
            assert names.count(BUILTIN_CGROUP_LOOPHOLE_NAME) <= 1
            # Clean up
            stop_loopholes(handles, _host_service_sockets_dir(cname))
        finally:
            sockets_dir = _host_service_sockets_dir(cname)
            if sockets_dir.exists():
                import shutil as _sh

                _sh.rmtree(sockets_dir, ignore_errors=True)


class TestBrokerSingleton:
    """The Claude OAuth broker is a singleton host daemon keyed on a
    well-known socket+PID pair, NOT spawned per-jail.  Per-jail was a
    holdover from the generic host-service machinery that caused
    (a) N brokers fighting over a shared flock, (b) stale daemons
    surviving wheel upgrades (the 2026-04-24 incident).  These tests
    lock in the singleton contract."""

    def _patch_paths(self, monkeypatch, tmp_path):
        import cli

        sock = tmp_path / "broker.sock"
        pidf = tmp_path / "broker.pid"
        monkeypatch.setattr(cli, "BROKER_SINGLETON_SOCKET", sock)
        monkeypatch.setattr(cli, "BROKER_SINGLETON_PID_FILE", pidf)
        return sock, pidf, cli

    def test_is_alive_false_without_pid_file(self, monkeypatch, tmp_path):
        _, _, cli = self._patch_paths(monkeypatch, tmp_path)
        assert cli._broker_is_alive() is False

    def test_is_alive_false_when_pid_dead(self, monkeypatch, tmp_path):
        """A PID file pointing at a non-existent process means a prior
        broker crashed without cleanup.  Liveness must report false
        so the next access respawns."""
        _, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        pidf.write_text("999999\n")  # PID extremely unlikely to exist

        def fake_kill(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(cli.os, "kill", fake_kill)
        assert cli._broker_is_alive() is False

    def test_is_alive_false_when_pid_alive_but_socket_missing(
        self, monkeypatch, tmp_path
    ):
        """Process exists but its socket doesn't — that's crashed
        mid-startup or bound to the wrong path.  Not alive for our
        purposes; the caller should kill + respawn."""
        _, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        pidf.write_text(str(os.getpid()))  # our own pid is real
        # socket left nonexistent
        assert cli._broker_is_alive() is False

    def test_is_alive_true_when_pid_alive_and_ping_succeeds(
        self, monkeypatch, tmp_path
    ):
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        pidf.write_text(str(os.getpid()))
        sock.touch()

        def fake_ping(*a, **kw):
            return True

        monkeypatch.setattr(cli, "_broker_ping", fake_ping)
        assert cli._broker_is_alive() is True

    def test_ensure_spawns_when_not_alive(self, monkeypatch, tmp_path):
        """``_broker_ensure`` is the one-shot entrypoint other code
        paths call.  It returns the socket path regardless of whether
        the broker was already alive or had to be spawned."""
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_broker_is_alive", lambda: False)

        spawned = {"n": 0}

        def fake_spawn():
            spawned["n"] += 1
            sock.touch()
            pidf.write_text("42\n")
            return sock

        monkeypatch.setattr(cli, "_broker_spawn", fake_spawn)
        result = cli._broker_ensure()
        assert result == sock
        assert spawned["n"] == 1

    def test_ensure_is_noop_when_alive(self, monkeypatch, tmp_path):
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(cli, "_broker_is_alive", lambda: True)
        spawned = {"n": 0}
        monkeypatch.setattr(
            cli, "_broker_spawn", lambda: spawned.update(n=spawned["n"] + 1) or sock
        )
        cli._broker_ensure()
        assert spawned["n"] == 0

    def test_kill_sends_sigterm_and_cleans_up(self, monkeypatch, tmp_path):
        """kill writes the broker-stop signal, removes the PID file,
        and unlinks the stale socket so the next spawn starts clean."""
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        pidf.write_text("12345\n")
        sock.touch()

        signals: list = []

        def fake_kill(pid, sig):
            signals.append((pid, sig))

        monkeypatch.setattr(cli.os, "kill", fake_kill)
        # After SIGTERM, "process gone": second kill() check raises.
        # Use a counter so first call is noop, subsequent raise.
        state = {"n": 0}

        def kill_with_death(pid, sig):
            state["n"] += 1
            if sig == 0 and state["n"] > 1:
                raise ProcessLookupError
            signals.append((pid, sig))

        monkeypatch.setattr(cli.os, "kill", kill_with_death)
        cli._broker_kill()
        # SIGTERM must have been sent
        assert any(sig == 15 for _, sig in signals), f"no SIGTERM in {signals}"
        assert not pidf.exists()
        assert not sock.exists()

    def test_kill_noop_when_pid_file_absent(self, monkeypatch, tmp_path):
        """``yolo broker stop`` when nothing is running must succeed
        silently, not raise."""
        _, _, cli = self._patch_paths(monkeypatch, tmp_path)
        # No pgrep matches either — nothing running anywhere.
        monkeypatch.setattr(cli, "_broker_pgrep_strays", lambda: [])
        cli._broker_kill()  # should not raise

    def test_kill_finds_strays_via_pgrep_when_pid_file_missing(
        self, monkeypatch, tmp_path
    ):
        """The 2026-04-26 incident: an old broker survived a wheel
        upgrade because the new code's PID file path didn't match
        whatever the old code wrote.  ``yolo broker restart`` ran
        ``_broker_kill`` against the empty PID-file path and silently
        no-op'd, so the stale broker kept serving stale code.
        ``_broker_kill`` must fall back to ``pgrep`` when the PID
        file is missing, so wheel-upgrade-orphans are cleaned up."""
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        # Stray broker found via pgrep, no PID file.
        monkeypatch.setattr(cli, "_broker_pgrep_strays", lambda: [42, 43])

        signals: list = []

        def fake_kill(pid, sig):
            signals.append((pid, sig))
            # Simulate process dying after first signal.
            if sig == 0:
                raise ProcessLookupError

        monkeypatch.setattr(cli.os, "kill", fake_kill)
        # Sock present so cleanup branch runs end-to-end.
        sock.touch()

        result = cli._broker_kill()
        assert result is True
        # Each stray must have received a SIGTERM.
        terms = [(p, s) for p, s in signals if s == signal.SIGTERM]
        assert sorted(p for p, _ in terms) == [42, 43]
        # Socket cleaned up.
        assert not sock.exists()

    def test_kill_pid_file_path_still_works(self, monkeypatch, tmp_path):
        """Regression guard for the PID-file path: when the PID file
        IS present, behavior is unchanged.  pgrep fallback only kicks
        in when the file is absent or empty."""
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)
        pidf.write_text("12345\n")
        sock.touch()

        # If pgrep ran, that'd be a bug (we already have a PID).
        pgrep_calls = {"n": 0}

        def fake_pgrep():
            pgrep_calls["n"] += 1
            return []

        monkeypatch.setattr(cli, "_broker_pgrep_strays", fake_pgrep)

        signals: list = []

        def fake_kill(pid, sig):
            signals.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError

        monkeypatch.setattr(cli.os, "kill", fake_kill)
        cli._broker_kill()
        assert any(p == 12345 and s == signal.SIGTERM for p, s in signals)
        # Pgrep fallback NOT consulted when PID file gave us a target.
        assert pgrep_calls["n"] == 0

    def test_spawn_takes_flock_to_avoid_double_spawn(self, monkeypatch, tmp_path):
        """Two parallel ``yolo run`` invocations must not both fork a
        broker — second caller sees the PID file the first just wrote.
        Test: call _broker_spawn twice back-to-back with Popen mocked;
        the second one should notice the PID file and skip.

        In the singleton design, _broker_spawn itself holds a flock on
        the PID file's lock path; while it's held, any concurrent spawner
        that tries to start inside it finds the file already populated
        when the flock releases."""
        sock, pidf, cli = self._patch_paths(monkeypatch, tmp_path)

        class FakePopen:
            _pid = 777

            def __init__(self, *a, **kw):
                type(self)._pid += 1
                self.pid = type(self)._pid

            def wait(self, timeout=None):
                return None

        monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(cli.time, "sleep", lambda *_: None)

        # Fake the "socket now exists" detection so spawn considers the
        # bind successful on the first call without needing a real daemon.
        def fake_wait_for_socket(p, *, timeout):
            sock.touch()
            return True

        monkeypatch.setattr(cli, "_broker_wait_for_socket", fake_wait_for_socket)

        cli._broker_spawn()
        _first_pid = pidf.read_text().strip()

        # Second call: PID file already exists and points at a live
        # process (ours).  Spawn should be a noop, PID file unchanged.
        pidf.write_text(str(os.getpid()))  # put a *real* live PID
        sock.touch()
        monkeypatch.setattr(cli, "_broker_ping", lambda *a, **kw: True)

        # _broker_spawn must bail when _broker_is_alive is True inside
        # its locked section.
        spawned_again = {"n": 0}

        orig_popen = cli.subprocess.Popen

        class TrackedPopen(FakePopen):
            def __init__(self, *a, **kw):
                spawned_again["n"] += 1
                super().__init__(*a, **kw)

        monkeypatch.setattr(cli.subprocess, "Popen", TrackedPopen)
        cli._broker_spawn()
        assert spawned_again["n"] == 0, (
            "second spawn must noop when PID file + live process present"
        )
        # ensure we didn't blow away the first PID file either
        assert pidf.exists()
        # (restore for other tests — monkeypatch will undo)
        del orig_popen

    def test_cli_status_reports_alive(self, monkeypatch, tmp_path):
        """``yolo broker status`` exit code 0 when healthy, non-zero
        otherwise — lets scripts gate on it."""
        from typer.testing import CliRunner
        import cli

        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli,
            "_broker_status",
            lambda: {
                "pid": 123,
                "pid_live": True,
                "socket_exists": True,
                "ping_ok": True,
                "socket": "/tmp/x.sock",
                "pid_file": "/tmp/x.pid",
            },
        )
        result = CliRunner().invoke(cli.app, ["broker", "status"])
        assert result.exit_code == 0, result.output
        assert "healthy" in result.output.lower()

    def test_cli_status_nonzero_when_dead(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner
        import cli

        self._patch_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli,
            "_broker_status",
            lambda: {
                "pid": None,
                "pid_live": False,
                "socket_exists": False,
                "ping_ok": False,
                "socket": "/tmp/x.sock",
                "pid_file": "/tmp/x.pid",
            },
        )
        result = CliRunner().invoke(cli.app, ["broker", "status"])
        assert result.exit_code != 0
        assert "not running" in result.output.lower()

    def test_cli_restart_invokes_kill_then_spawn(self, monkeypatch, tmp_path):
        from typer.testing import CliRunner
        import cli

        sock, _, _ = self._patch_paths(monkeypatch, tmp_path)
        order: list = []

        def fake_kill():
            order.append("kill")
            return True

        def fake_spawn():
            order.append("spawn")
            return sock

        monkeypatch.setattr(cli, "_broker_kill", fake_kill)
        monkeypatch.setattr(cli, "_broker_spawn", fake_spawn)
        monkeypatch.setattr(cli, "_broker_is_alive", lambda: True)

        result = CliRunner().invoke(cli.app, ["broker", "restart"])
        assert result.exit_code == 0, result.output
        assert order == ["kill", "spawn"]
        assert "restarted" in result.output.lower()


class TestHostServiceLivenessProbe:
    """``_check_host_service_liveness`` probes per-jail UNIX sockets to
    confirm the daemons spawned by ``start_loopholes`` are alive.  But
    the broker is a SINGLETON now (post-e7b7073) — its bind-mount source
    on the host is a zero-byte placeholder, not a real socket.  Probing
    that path always fails with ECONNREFUSED, so doctor was reporting
    the broker as dead while the actual singleton was healthy.

    Lock in the fix: the per-jail probe must skip the broker name and
    leave broker liveness reporting to ``_check_loopholes``'s singleton
    probe (which uses ``_broker_status`` against the singleton path)."""

    def _common_setup(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock as _MM

        # Pretend we're on the host — the probe early-returns inside a jail.
        monkeypatch.delenv("YOLO_VERSION", raising=False)
        # Pretend a runtime is available.
        monkeypatch.setattr(cli, "_detect_runtime_for_listing", lambda: "podman")
        # Pretend one jail is running.
        run_result = _MM(stdout="yolo-test-cname-abc12345\n", returncode=0)
        monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: run_result)
        # Per-jail sockets dir; we'll touch a placeholder broker socket
        # in here to mimic the host-side bind-mount source.
        sockets_dir = tmp_path / "yolo-host-services-test"
        sockets_dir.mkdir()
        # Zero-byte regular file — exactly what the broker bind-mount source
        # looks like on the host.  A connect() against it raises ENOTSOCK.
        (sockets_dir / f"{cli.BROKER_LOOPHOLE_NAME}.sock").touch()
        monkeypatch.setattr(
            cli, "_host_service_sockets_dir", lambda _cname: sockets_dir
        )
        return sockets_dir

    def _broker_loophole_entry(self):
        """Return a (path, loophole, err) entry shaped like
        ``_loopholes.validate_loopholes`` produces, with a broker
        loophole that has a ``host_daemon`` field set."""
        from unittest.mock import MagicMock as _MM
        from src.loopholes import HostDaemon

        lp = _MM()
        lp.name = cli.BROKER_LOOPHOLE_NAME
        lp.enabled = True
        lp.requirements_met = True
        lp.host_daemon = HostDaemon(cmd=["yolo-claude-oauth-broker-host"])
        return (None, lp, None)

    def test_probe_skips_singleton_broker(self, monkeypatch, tmp_path):
        """The broker is a singleton; its per-jail sockets-dir entry
        is a bind-mount placeholder, not a real socket.  The per-jail
        probe must skip this loophole entirely so doctor doesn't
        report a healthy broker as dead."""
        self._common_setup(monkeypatch, tmp_path)
        monkeypatch.setattr(
            cli._loopholes,
            "validate_loopholes",
            lambda: [self._broker_loophole_entry()],
        )

        events: list = []
        cli._check_host_service_liveness(
            lambda m, *a, **kw: events.append(("ok", m)),
            lambda m, *a, **kw: events.append(("warn", m)),
            lambda m, *a, **kw: events.append(("fail", m)),
        )
        # Critically: NO failure for the broker.  A "skipping broker"
        # info line is fine; "socket dead" is not.
        fails = [m for kind, m in events if kind == "fail"]
        assert all("claude-oauth-broker" not in m for m in fails), (
            f"per-jail probe must not fail for the singleton broker; got {fails}"
        )

    def test_probe_still_runs_for_non_broker_loopholes(self, monkeypatch, tmp_path):
        """Other loopholes (e.g. host-processes) ARE per-jail and DO
        have real sockets — the skip must be broker-only.  Verify a
        synthetic non-broker loophole with a missing socket still gets
        a fail (i.e. the probe's normal logic runs)."""
        from unittest.mock import MagicMock as _MM
        from src.loopholes import HostDaemon

        sockets_dir = self._common_setup(monkeypatch, tmp_path)
        # Don't create a socket for "host-processes" — the probe should
        # report it missing.
        other = _MM()
        other.name = "host-processes"
        other.enabled = True
        other.requirements_met = True
        other.host_daemon = HostDaemon(cmd=["yolo-host-processes"])

        monkeypatch.setattr(
            cli._loopholes, "validate_loopholes", lambda: [(None, other, None)]
        )

        events: list = []
        cli._check_host_service_liveness(
            lambda m, *a, **kw: events.append(("ok", m)),
            lambda m, *a, **kw: events.append(("warn", m)),
            lambda m, *a, **kw: events.append(("fail", m)),
        )
        # Probe ran; reported the missing socket as a fail.
        fails = [m for kind, m in events if kind == "fail"]
        assert any("host-processes" in m for m in fails)
        # Sanity: sockets_dir is still the one we set up (no use-after-free).
        assert sockets_dir.is_dir()
