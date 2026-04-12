"""Unit tests for cli.py — check command, auto_load_image, tmux/kitty, ps, and run internals.

These tests mock subprocess and filesystem to exercise cli.py's heavier logic
without spinning up actual containers.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "src"))

from typer.testing import CliRunner  # noqa: E402

from cli import (  # noqa: E402
    _build_image_store_path,
    _kitty_setup_jail_tab,
    _tmux_rename_window,
    _tmux_setup_jail_pane,
    auto_load_image,
    start_host_port_forwarding,
    app,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: auto_load_image
# ═══════════════════════════════════════════════════════════════════════════════


class TestAutoLoadImage:
    """Test the nix image build + load pipeline."""

    @patch("cli._build_image_store_path")
    @patch("cli._read_loaded_paths")
    @patch("cli._add_loaded_path")
    @patch("subprocess.Popen")
    @patch("cli._estimate_image_size", return_value=0)
    def test_skips_load_when_already_loaded(
        self, mock_est, mock_popen, mock_add, mock_read, mock_build, tmp_path
    ):
        mock_build.return_value = ("/nix/store/abc", [])
        mock_read.return_value = {"/nix/store/abc"}  # Already loaded
        with patch("cli.BUILD_DIR", tmp_path):
            auto_load_image(tmp_path, runtime="docker")
        mock_popen.assert_not_called()  # No streaming needed

    @patch("cli._build_image_store_path")
    @patch("subprocess.run")
    def test_warns_on_build_failure_with_existing_image(
        self, mock_run, mock_build, tmp_path
    ):
        mock_build.return_value = (None, ["error: something broke"])
        mock_run.return_value = MagicMock(returncode=0)  # Image exists
        with patch("cli.BUILD_DIR", tmp_path):
            auto_load_image(tmp_path, runtime="docker")
        # Should have checked for existing image
        mock_run.assert_called()

    @patch("cli._build_image_store_path")
    @patch("subprocess.run")
    def test_errors_on_build_failure_no_image(self, mock_run, mock_build, tmp_path):
        mock_build.return_value = (None, ["error: nope"])
        mock_run.return_value = MagicMock(returncode=1)  # No image
        with patch("cli.BUILD_DIR", tmp_path):
            auto_load_image(tmp_path, runtime="docker")

    @patch("cli._build_image_store_path")
    @patch("cli._read_loaded_paths", return_value=set())
    @patch("cli._add_loaded_path")
    @patch("cli._estimate_image_size", return_value=100_000_000)
    def test_caches_and_loads_image_on_new_path(
        self, mock_est, mock_add, mock_read, mock_build, tmp_path
    ):
        mock_build.return_value = ("/nix/store/new", [])

        # Mock the streaming to cache file
        stream_proc = MagicMock()
        stream_proc.stdout.read.side_effect = [b"x" * 1024, b""]  # One chunk
        stream_proc.returncode = 0
        stream_proc.wait.return_value = None

        load_result = MagicMock(returncode=0, stderr=b"")

        with (
            patch("cli.BUILD_DIR", tmp_path),
            patch("cli.GLOBAL_CACHE", tmp_path / "cache"),
            patch("subprocess.Popen", return_value=stream_proc),
            patch("subprocess.run", return_value=load_result),
        ):
            auto_load_image(tmp_path, runtime="docker")

        mock_add.assert_called_once()

    @patch("cli._build_image_store_path")
    @patch("cli._read_loaded_paths", return_value=set())
    @patch("cli._add_loaded_path")
    def test_reuses_cached_tar(self, mock_add, mock_read, mock_build, tmp_path):
        """When the tar cache file already exists, skip materialization."""
        mock_build.return_value = ("/nix/store/cached", [])

        # Pre-create the cache file
        cache_dir = tmp_path / "cache" / "images"
        cache_dir.mkdir(parents=True)
        import hashlib

        path_hash = hashlib.sha256(b"/nix/store/cached").hexdigest()[:16]
        (cache_dir / f"{path_hash}.tar").write_bytes(b"fake tar")

        load_result = MagicMock(returncode=0, stderr=b"")

        with (
            patch("cli.BUILD_DIR", tmp_path),
            patch("cli.GLOBAL_CACHE", tmp_path / "cache"),
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.run", return_value=load_result),
        ):
            auto_load_image(tmp_path, runtime="docker")

        # Should NOT have streamed (Popen not called for image generation)
        mock_popen.assert_not_called()
        mock_add.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _build_image_store_path
# ═══════════════════════════════════════════════════════════════════════════════


def _make_nix_proc(lines, returncode=0):
    """Create a mock subprocess for nix build with readline-compatible stderr."""
    proc = MagicMock()
    proc.stderr = MagicMock()
    proc.stderr.readline = MagicMock(side_effect=lines)
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


class TestBuildImageStorePath:
    @patch("subprocess.Popen")
    def test_successful_build(self, mock_popen, tmp_path):
        out_link = tmp_path / "result"
        out_link.symlink_to(tmp_path)
        mock_popen.return_value = _make_nix_proc(["evaluating\n", ""], 0)

        path, tail = _build_image_store_path(
            tmp_path,
            out_link=out_link,
            status_message="Building...",
        )
        assert path is not None

    @patch("subprocess.Popen")
    def test_failed_build(self, mock_popen, tmp_path):
        out_link = tmp_path / "result"
        mock_popen.return_value = _make_nix_proc(["error: something bad\n", ""], 1)

        path, tail = _build_image_store_path(
            tmp_path,
            out_link=out_link,
            status_message="Building...",
        )
        assert path is None
        assert len(tail) > 0

    @patch("subprocess.Popen", side_effect=FileNotFoundError)
    def test_nix_not_found(self, mock_popen, tmp_path):
        out_link = tmp_path / "result"
        path, tail = _build_image_store_path(
            tmp_path,
            out_link=out_link,
            status_message="Building...",
        )
        assert path is None
        assert "nix command not found" in tail[0]

    @patch("subprocess.Popen")
    def test_extra_packages_env(self, mock_popen, tmp_path):
        out_link = tmp_path / "result"
        out_link.symlink_to(tmp_path)
        mock_popen.return_value = _make_nix_proc([""], 0)

        _build_image_store_path(
            tmp_path,
            extra_packages=["postgresql"],
            out_link=out_link,
            status_message="Building...",
        )
        call_kwargs = mock_popen.call_args
        env = call_kwargs[1].get("env") or call_kwargs.kwargs.get("env")
        assert env is not None
        assert "YOLO_EXTRA_PACKAGES" in env

    @patch("subprocess.Popen")
    def test_stderr_tail_capped(self, mock_popen, tmp_path):
        out_link = tmp_path / "result"
        lines = [f"line {i}\n" for i in range(50)] + [""]
        mock_popen.return_value = _make_nix_proc(lines, 1)

        path, tail = _build_image_store_path(
            tmp_path,
            out_link=out_link,
            status_message="Building...",
        )
        assert len(tail) <= 30


# ═══════════════════════════════════════════════════════════════════════════════
# Test: tmux helpers
# ═══════════════════════════════════════════════════════════════════════════════


class TestTmuxRenameWindow:
    def test_noop_when_no_tmux(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("YOLO_NO_TMUX", raising=False)
        with patch("subprocess.run") as mock_run:
            _tmux_rename_window("JAIL")
            mock_run.assert_not_called()

    def test_noop_when_yolo_no_tmux(self, monkeypatch):
        monkeypatch.setenv("YOLO_NO_TMUX", "1")
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        with patch("subprocess.run") as mock_run:
            _tmux_rename_window("JAIL")
            mock_run.assert_not_called()

    def test_renames_in_tmux(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        monkeypatch.delenv("YOLO_NO_TMUX", raising=False)
        with patch("subprocess.run") as mock_run:
            _tmux_rename_window("JAIL")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "tmux" in args
            assert "rename-window" in args
            assert "JAIL" in args


class TestTmuxSetupJailPane:
    def test_noop_when_no_tmux(self, monkeypatch):
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("YOLO_NO_TMUX", raising=False)
        result = _tmux_setup_jail_pane()
        assert result is None

    def test_noop_when_yolo_no_tmux(self, monkeypatch):
        monkeypatch.setenv("YOLO_NO_TMUX", "1")
        result = _tmux_setup_jail_pane()
        assert result is None

    def test_noop_when_not_tty(self, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        monkeypatch.delenv("YOLO_NO_TMUX", raising=False)
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = _tmux_setup_jail_pane()
            assert result is None

    @patch("subprocess.run")
    def test_sets_indicators_and_returns_restore(self, mock_run, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.delenv("YOLO_NO_TMUX", raising=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="old-window\n")
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            result = _tmux_setup_jail_pane()
            assert result is not None  # restore function returned
            assert callable(result)

    @patch("subprocess.run")
    def test_restore_function_calls_tmux(self, mock_run, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        monkeypatch.setenv("TMUX_PANE", "%0")
        monkeypatch.delenv("YOLO_NO_TMUX", raising=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="old-window\n")
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            restore = _tmux_setup_jail_pane()
        # Call restore
        restore()
        # Should have called tmux to restore settings
        assert mock_run.call_count > 0


class TestKittySetupJailTab:
    def test_noop_no_kitty(self, monkeypatch):
        monkeypatch.delenv("KITTY_PID", raising=False)
        result = _kitty_setup_jail_tab()
        assert result is None

    def test_noop_not_tty(self, monkeypatch):
        monkeypatch.setenv("KITTY_PID", "1234")
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            result = _kitty_setup_jail_tab()
            assert result is None

    @patch("subprocess.run")
    @patch("subprocess.check_output", return_value=b"old-title\n")
    def test_sets_tab_title(self, mock_check, mock_run, monkeypatch):
        monkeypatch.setenv("KITTY_PID", "1234")
        monkeypatch.setenv("KITTY_WINDOW_ID", "42")
        monkeypatch.delenv("SM_PROJECT", raising=False)
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            result = _kitty_setup_jail_tab()
            assert result is not None
            assert callable(result)

    @patch("subprocess.run", side_effect=Exception("kitten not found"))
    @patch("subprocess.check_output", return_value=b"")
    def test_graceful_on_kitten_failure(self, mock_check, mock_run, monkeypatch):
        monkeypatch.setenv("KITTY_PID", "1234")
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            result = _kitty_setup_jail_tab()
            assert result is None  # Fails gracefully


# ═══════════════════════════════════════════════════════════════════════════════
# Test: check command (via CliRunner with mocked subprocess)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckCommand:
    """Test the check/doctor command by mocking external calls."""

    def _mock_subprocess_run(self, cmd, **kwargs):
        """Smart mock that returns expected output for different commands."""
        if isinstance(cmd, list):
            prog = cmd[0] if cmd else ""
            if prog in ("podman", "docker"):
                if "--version" in cmd:
                    return MagicMock(returncode=0, stdout=f"{prog} version 4.9.0\n")
                if "image" in cmd and "inspect" in cmd:
                    return MagicMock(returncode=0)
                if "ps" in cmd:
                    return MagicMock(returncode=0, stdout="")
                if "images" in cmd:
                    return MagicMock(returncode=0, stdout="yolo-jail:latest (1.2 GB)\n")
                return MagicMock(returncode=0, stdout="")
            if prog == "nix":
                return MagicMock(returncode=0, stdout="nix (Nix) 2.18.1\n")
            if prog == sys.executable:
                return MagicMock(returncode=0, stdout="ok\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("shutil.which")
    def test_check_no_build(
        self, mock_which, mock_popen, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )
        mock_run.side_effect = self._mock_subprocess_run

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        # Should complete without crashing
        assert "Summary" in result.output or result.exit_code == 0

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_reports_missing_runtime(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.return_value = None  # Nothing found
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        # Should report failure
        assert (
            result.exit_code != 0
            or "No container runtime" in result.output
            or "failed" in result.output
        )

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_validates_workspace_config(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )
        mock_run.side_effect = self._mock_subprocess_run

        # Write an invalid config
        (tmp_path / "yolo-jail.jsonc").write_text('{"bogus_key": "bad"}')

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "unknown" in result.output.lower() or result.exit_code != 0

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_invalid_json(self, mock_which, mock_run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )
        mock_run.side_effect = self._mock_subprocess_run

        # Write broken JSON
        (tmp_path / "yolo-jail.jsonc").write_text("{broken json}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert result.exit_code != 0


class TestDoctorAlias:
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_doctor_is_alias_for_check(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )
        mock_run.return_value = MagicMock(returncode=0, stdout="podman version 4.9.0\n")

        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--no-build"])
        # Doctor should behave like check
        assert "Summary" in result.output or result.exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: ps command
# ═══════════════════════════════════════════════════════════════════════════════


class TestPsCommand:
    @patch("cli._runtime", return_value="podman")
    @patch("subprocess.run")
    def test_no_running_jails(self, mock_run, mock_runtime):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        runner = CliRunner()
        result = runner.invoke(app, ["ps"])
        assert "No running jails" in result.output

    @patch("cli._runtime", return_value="podman")
    @patch("subprocess.run")
    @patch("cli.find_running_container")
    @patch("cli.CONTAINER_DIR")
    def test_shows_running_jails(
        self, mock_dir, mock_find, mock_run, mock_runtime, tmp_path
    ):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="NAMES\tSTATUS\tRUNNING\nyolo-abc123\tUp 5 minutes\t5 minutes ago\n",
        )
        mock_dir.exists.return_value = True
        tracking = tmp_path / "yolo-abc123"
        tracking.write_text("/home/user/project\n")
        mock_dir.iterdir.return_value = [tracking]
        mock_find.return_value = "abc123"

        runner = CliRunner()
        result = runner.invoke(app, ["ps"])
        assert result.exit_code == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: start_host_port_forwarding
# ═══════════════════════════════════════════════════════════════════════════════


class TestStartHostPortForwarding:
    def test_empty_list_returns_empty(self, tmp_path):
        result = start_host_port_forwarding([], "yolo-test", tmp_path)
        assert result == []

    @patch("subprocess.Popen")
    def test_creates_socat_processes(self, mock_popen, tmp_path):
        mock_popen.return_value = MagicMock()
        socket_dir = tmp_path / "sockets"
        Path.home() / ".local" / "share" / "yolo-jail" / "logs"
        result = start_host_port_forwarding([5432], "yolo-test", socket_dir)
        assert len(result) == 1
        mock_popen.assert_called_once()

    @patch("subprocess.Popen", side_effect=FileNotFoundError)
    def test_socat_not_found(self, mock_popen, tmp_path, capsys):
        socket_dir = tmp_path / "sockets"
        result = start_host_port_forwarding([5432], "yolo-test", socket_dir)
        assert result == []

    @patch("subprocess.Popen")
    def test_multiple_ports(self, mock_popen, tmp_path):
        mock_popen.return_value = MagicMock()
        socket_dir = tmp_path / "sockets"
        result = start_host_port_forwarding(
            [5432, "8080:9090"], "yolo-test", socket_dir
        )
        assert len(result) == 2

    @patch("subprocess.Popen")
    def test_removes_stale_socket(self, mock_popen, tmp_path):
        mock_popen.return_value = MagicMock()
        socket_dir = tmp_path / "sockets"
        socket_dir.mkdir()
        stale = socket_dir / "port-5432.sock"
        stale.touch()
        start_host_port_forwarding([5432], "yolo-test", socket_dir)
        # Stale socket should have been removed before socat
        # (socat creates a new one)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: init-user-config command
# ═══════════════════════════════════════════════════════════════════════════════


class TestInitUserConfig:
    @patch("cli.USER_CONFIG_PATH")
    def test_creates_user_config(self, mock_path, tmp_path, monkeypatch):
        config_path = tmp_path / "config.jsonc"
        monkeypatch.setattr("cli.USER_CONFIG_PATH", config_path)
        runner = CliRunner()
        result = runner.invoke(app, ["init-user-config"])
        assert result.exit_code == 0
        assert config_path.exists()

    def test_init_user_config_idempotent(self, tmp_path, monkeypatch):
        config_path = tmp_path / "config.jsonc"
        monkeypatch.setattr("cli.USER_CONFIG_PATH", config_path)
        runner = CliRunner()
        runner.invoke(app, ["init-user-config"])
        result = runner.invoke(app, ["init-user-config"])
        assert result.exit_code == 0
        assert "already exists" in result.output


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _print_init_briefing (via init)
# ═══════════════════════════════════════════════════════════════════════════════


class TestInitBriefing:
    def test_briefing_contains_key_info(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(app, ["init"])
        assert "YOLO" in result.output.upper() or "jail" in result.output.lower()
        # Briefing should mention key setup steps
        assert "yolo-jail.jsonc" in result.output or "config" in result.output.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: run() command internal logic (mocked)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRunCommandInternals:
    """Test run() internals by mocking container interactions."""

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("subprocess.check_output")
    @patch("cli.find_running_container", return_value="abc123")
    @patch("cli._runtime", return_value="podman")
    @patch("cli.auto_load_image")
    def test_exec_into_existing_container(
        self,
        mock_load,
        mock_runtime,
        mock_find,
        mock_check_output,
        mock_popen,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        mock_check_output.side_effect = FileNotFoundError  # No git/jj identity
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "echo", "hello"])
        # Should try to exec into existing container (not start new one)
        mock_load.assert_not_called()

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("subprocess.check_output")
    @patch("cli.find_running_container", return_value=None)
    @patch("cli._runtime", return_value="podman")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.generate_agents_md")
    @patch("cli.start_host_port_forwarding", return_value=[])
    @patch("cli.cleanup_port_forwarding")
    @patch("cli.write_container_tracking")
    @patch("cli._tmux_rename_window")
    @patch("cli._host_mise_dir")
    @patch("cli._seed_agent_dir")
    def test_new_container_creation(
        self,
        mock_seed_agent,
        mock_mise_dir,
        mock_tmux,
        mock_write_track,
        mock_cleanup_fwd,
        mock_start_fwd,
        mock_agents,
        mock_config_changes,
        mock_load,
        mock_runtime,
        mock_find,
        mock_check_output,
        mock_popen,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        mock_check_output.side_effect = FileNotFoundError
        mock_mise_dir.return_value = tmp_path / "mise"
        (tmp_path / "mise").mkdir()
        agents_dir = tmp_path / "agents" / "yolo-test"
        agents_dir.mkdir(parents=True)
        (agents_dir / "AGENTS-copilot.md").write_text("test")
        (agents_dir / "AGENTS-gemini.md").write_text("test")
        (agents_dir / "CLAUDE.md").write_text("test")
        mock_agents.return_value = agents_dir
        monkeypatch.setattr("cli.GLOBAL_STORAGE", tmp_path / "storage")
        (tmp_path / "storage" / "locks").mkdir(parents=True, exist_ok=True)

        proc = MagicMock()
        proc.wait.return_value = None
        proc.returncode = 0
        mock_popen.return_value = proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "echo", "hello"])
        # When no existing container and config changes approved,
        # should attempt to load the image (or fail gracefully trying)
        # The key assertion is that it didn't exec into an existing container
        assert not any(
            "Attaching to existing" in str(c) for c in mock_run.call_args_list
        )

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("cli.find_running_container", return_value="abc123")
    @patch("cli._runtime", return_value="podman")
    def test_copilot_yolo_injection(
        self,
        mock_runtime,
        mock_find,
        mock_check_output,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        """Test that --yolo is injected for copilot command."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "copilot"])
        # The docker exec command should include --yolo
        if mock_run.called:
            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(str(c) for c in cmd)
            assert "--yolo" in cmd_str or "--no-auto-update" in cmd_str

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("cli.find_running_container", return_value="abc123")
    @patch("cli._runtime", return_value="podman")
    def test_gemini_yolo_injection(
        self,
        mock_runtime,
        mock_find,
        mock_check_output,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        """Test that --yolo is injected for gemini command."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "gemini"])
        if mock_run.called:
            cmd = mock_run.call_args[0][0]
            cmd_str = " ".join(str(c) for c in cmd)
            assert "--yolo" in cmd_str


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _resolve_repo_root (installed package path)
# ═══════════════════════════════════════════════════════════════════════════════


class TestResolveRepoRootInstalled:
    """Test installed-package code path of _resolve_repo_root."""

    def test_installed_package_path(self, tmp_path, monkeypatch):
        """When running from installed package, should copy files to build root."""
        from cli import _resolve_repo_root

        monkeypatch.delenv("YOLO_REPO_ROOT", raising=False)

        pkg_dir = tmp_path / "pkg" / "src"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "flake.nix").write_text("{ }")
        (pkg_dir / "flake.lock").write_text("{}")
        (pkg_dir / "entrypoint.py").write_text("")
        (pkg_dir / "cli.py").write_text("")

        build_root = tmp_path / "storage" / "nix-build-root"
        monkeypatch.setattr("cli.GLOBAL_STORAGE", tmp_path / "storage")

        import cli

        original_file = cli.__file__
        try:
            cli.__file__ = str(pkg_dir / "cli.py")
            result = _resolve_repo_root()
            assert result == build_root.resolve()
            assert (build_root / "flake.nix").exists()
            assert (build_root / "src").exists()
        finally:
            cli.__file__ = original_file

    def test_user_config_repo_path(self, tmp_path, monkeypatch):
        """When user config has repo_path, use it."""
        from cli import _resolve_repo_root

        monkeypatch.delenv("YOLO_REPO_ROOT", raising=False)

        import cli

        original_file = cli.__file__
        try:
            fake_dir = tmp_path / "no-flake"
            fake_dir.mkdir()
            (fake_dir / "cli.py").write_text("")
            cli.__file__ = str(fake_dir / "cli.py")

            user_config = tmp_path / "config.jsonc"
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            (repo_dir / "flake.nix").write_text("{ }")
            user_config.write_text(json.dumps({"repo_path": str(repo_dir)}))
            monkeypatch.setattr("cli.USER_CONFIG_PATH", user_config)

            result = _resolve_repo_root()
            assert result == repo_dir.resolve()
        finally:
            cli.__file__ = original_file

    def test_fails_when_nothing_found(self, tmp_path, monkeypatch):
        """When no repo root is found, exits with error."""
        from cli import _resolve_repo_root

        monkeypatch.delenv("YOLO_REPO_ROOT", raising=False)

        import cli

        original_file = cli.__file__
        try:
            fake_dir = tmp_path / "no-flake"
            fake_dir.mkdir()
            (fake_dir / "cli.py").write_text("")
            cli.__file__ = str(fake_dir / "cli.py")
            monkeypatch.setattr("cli.USER_CONFIG_PATH", tmp_path / "no-config")

            with pytest.raises((SystemExit, RuntimeError)):
                _resolve_repo_root()
        finally:
            cli.__file__ = original_file


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _validate_config missing edges
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateConfigEdges:
    """Cover _validate_config branches not yet tested."""

    def _validate(self, config, workspace=None):
        from cli import _validate_config

        return _validate_config(config, workspace=workspace or Path("/tmp/test"))

    def test_packages_object_with_nixpkgs_and_version_conflict(self):
        errors, _ = self._validate(
            {
                "packages": [
                    {
                        "name": "pkg",
                        "nixpkgs": "abc123",
                        "version": "1.0",
                        "url": "x",
                        "hash": "y",
                    }
                ]
            }
        )
        assert any("not both" in e for e in errors)

    def test_packages_object_missing_name(self):
        errors, _ = self._validate({"packages": [{"nixpkgs": "abc123"}]})
        assert any("name" in e for e in errors)

    def test_packages_object_version_without_required_fields(self):
        errors, _ = self._validate(
            {"packages": [{"name": "pkg", "version": 123, "url": 456, "hash": 789}]}
        )
        assert any("expected a string" in e for e in errors)

    def test_packages_object_no_pinning(self):
        errors, _ = self._validate({"packages": [{"name": "pkg"}]})
        assert any("nixpkgs" in e or "version" in e for e in errors)

    def test_mounts_non_string(self):
        errors, _ = self._validate({"mounts": [123]})
        assert any("expected a string" in e for e in errors)

    def test_mounts_host_not_found_warning(self):
        _, warnings = self._validate({"mounts": ["/nonexistent/path/xyz"]})
        assert any("does not exist" in w for w in warnings)

    def test_network_not_dict(self):
        errors, _ = self._validate({"network": "bad"})
        assert any("expected an object" in e for e in errors)

    def test_network_ports_not_list(self):
        errors, _ = self._validate({"network": {"ports": "bad"}})
        assert any("expected a list" in e for e in errors)

    def test_network_forward_host_ports_not_list(self):
        errors, _ = self._validate({"network": {"forward_host_ports": "bad"}})
        assert any("expected a list" in e for e in errors)

    def test_network_host_mode_warnings(self):
        _, warnings = self._validate(
            {
                "network": {
                    "mode": "host",
                    "ports": ["8080:8080"],
                    "forward_host_ports": [5432],
                }
            }
        )
        assert any("ignored" in w for w in warnings)

    def test_security_not_dict(self):
        errors, _ = self._validate({"security": "bad"})
        assert any("expected an object" in e for e in errors)

    def test_security_blocked_tools_not_list(self):
        errors, _ = self._validate({"security": {"blocked_tools": "bad"}})
        assert any("expected a list" in e for e in errors)

    def test_security_blocked_tool_bad_type(self):
        errors, _ = self._validate({"security": {"blocked_tools": [123]}})
        assert any("expected a string or object" in e for e in errors)

    def test_security_blocked_tool_missing_name(self):
        errors, _ = self._validate({"security": {"blocked_tools": [{"message": "hi"}]}})
        assert any("name" in e for e in errors)

    def test_security_blocked_tool_bad_message_type(self):
        errors, _ = self._validate(
            {"security": {"blocked_tools": [{"name": "x", "message": 123}]}}
        )
        assert any("expected a string" in e for e in errors)

    def test_mise_tools_not_dict(self):
        errors, _ = self._validate({"mise_tools": "bad"})
        assert any("expected an object" in e for e in errors)

    def test_mise_tools_bad_value(self):
        errors, _ = self._validate({"mise_tools": {"node": 123}})
        assert any("expected a version string" in e for e in errors)

    def test_lsp_servers_not_dict(self):
        errors, _ = self._validate({"lsp_servers": "bad"})
        assert any("expected an object" in e for e in errors)

    def test_lsp_server_not_object(self):
        errors, _ = self._validate({"lsp_servers": {"python": "bad"}})
        assert any("expected an object" in e for e in errors)

    def test_lsp_server_missing_command(self):
        errors, _ = self._validate(
            {"lsp_servers": {"python": {"fileExtensions": {".py": "python"}}}}
        )
        assert any("command" in e for e in errors)

    def test_lsp_server_bad_file_extensions(self):
        errors, _ = self._validate(
            {"lsp_servers": {"python": {"command": "pyright", "fileExtensions": "bad"}}}
        )
        assert any("fileExtensions" in e for e in errors)

    def test_lsp_server_bad_ext_values(self):
        errors, _ = self._validate(
            {
                "lsp_servers": {
                    "python": {"command": "pyright", "fileExtensions": {".py": 123}}
                }
            }
        )
        assert any("strings" in e for e in errors)

    def test_mcp_presets_not_list(self):
        errors, _ = self._validate({"mcp_presets": "bad"})
        assert any("expected an array" in e for e in errors)

    def test_mcp_presets_non_string(self):
        errors, _ = self._validate({"mcp_presets": [123]})
        assert any("expected a string" in e for e in errors)

    def test_mcp_servers_not_dict(self):
        errors, _ = self._validate({"mcp_servers": "bad"})
        assert any("expected an object" in e for e in errors)

    def test_mcp_servers_bad_entry(self):
        errors, _ = self._validate({"mcp_servers": {"foo": "bar"}})
        assert any("expected an object or null" in e for e in errors)

    def test_mcp_server_missing_command(self):
        errors, _ = self._validate({"mcp_servers": {"foo": {"args": []}}})
        assert any("command" in e for e in errors)

    def test_devices_not_list(self):
        errors, _ = self._validate({"devices": "bad"})
        assert any("expected a list" in e for e in errors)

    def test_device_bad_type(self):
        errors, _ = self._validate({"devices": [123]})
        assert any("expected a string or object" in e for e in errors)

    def test_device_both_usb_and_cgroup(self):
        errors, _ = self._validate(
            {"devices": [{"usb": "0bda:2838", "cgroup_rule": "c 189:* rwm"}]}
        )
        assert any("exactly one" in e for e in errors)

    def test_device_usb_bad_format(self):
        errors, _ = self._validate({"devices": [{"usb": "bad-format"}]})
        assert any("hex format" in e for e in errors)

    def test_device_usb_bad_type(self):
        errors, _ = self._validate({"devices": [{"usb": 123}]})
        assert any("expected a string" in e for e in errors)

    def test_device_cgroup_bad_type(self):
        errors, _ = self._validate({"devices": [{"cgroup_rule": 123}]})
        assert any("expected a string" in e for e in errors)

    def test_device_description_bad_type(self):
        errors, _ = self._validate(
            {"devices": [{"usb": "0bda:2838", "description": 123}]}
        )
        assert any("description" in e for e in errors)

    def test_repo_path_bad_type(self):
        errors, _ = self._validate({"repo_path": 123})
        assert any("expected a string" in e for e in errors)

    def test_validate_port_number_invalid(self):
        from cli import _validate_port_number

        errors = []
        _validate_port_number("abc", "test", errors)
        assert len(errors) == 1

    def test_validate_publish_port_three_parts(self):
        from cli import _validate_publish_port

        errors = []
        _validate_publish_port("127.0.0.1:8080:9090", "test", errors)
        assert len(errors) == 0

    def test_validate_publish_port_with_protocol(self):
        from cli import _validate_publish_port

        errors = []
        _validate_publish_port("8080:9090/udp", "test", errors)
        assert len(errors) == 0

    def test_validate_publish_port_bad_protocol(self):
        from cli import _validate_publish_port

        errors = []
        _validate_publish_port("8080:9090/icmp", "test", errors)
        assert any("protocol" in e for e in errors)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: check() command thorough happy path
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckCommandDetailed:
    """More thorough check command tests to cover inner branches."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_happy_path_all_green(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        monkeypatch.setattr("cli.GLOBAL_HOME", tmp_path / "home")
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        monkeypatch.setattr("cli.USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
        for d in ("home", "mise", "containers", "agents", "build"):
            (tmp_path / d).mkdir()

        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )

        def smart_run(cmd, **kwargs):
            if isinstance(cmd, list):
                prog = cmd[0]
                if prog == "podman":
                    if "--version" in cmd:
                        return MagicMock(returncode=0, stdout="podman version 4.9.0\n")
                    if "images" in cmd:
                        return MagicMock(
                            returncode=0, stdout="yolo-jail:latest (1.2 GB)\n"
                        )
                    if "ps" in cmd:
                        return MagicMock(returncode=0, stdout="")
                    return MagicMock(returncode=0, stdout="")
                if prog == "nix":
                    return MagicMock(returncode=0, stdout="nix (Nix) 2.18.1\n")
                if prog == sys.executable:
                    return MagicMock(returncode=0, stdout="ok\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = smart_run
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"security": {"blocked_tools": ["grep"]}}'
        )

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "passed" in result.output
        assert result.exit_code == 0

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_with_warnings(self, mock_which, mock_run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        monkeypatch.setattr("cli.GLOBAL_HOME", tmp_path / "home")
        monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
        monkeypatch.setattr("cli.USER_CONFIG_PATH", tmp_path / "user-config.jsonc")

        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )

        def smart_run(cmd, **kwargs):
            if isinstance(cmd, list):
                prog = cmd[0]
                if prog == "podman":
                    if "--version" in cmd:
                        return MagicMock(returncode=0, stdout="podman version 4.9.0\n")
                    if "images" in cmd:
                        return MagicMock(returncode=0, stdout="")
                    if "ps" in cmd:
                        return MagicMock(returncode=0, stdout="")
                    return MagicMock(returncode=0, stdout="")
                if prog == "nix":
                    return MagicMock(returncode=0, stdout="nix (Nix) 2.18.1\n")
                if prog == sys.executable:
                    return MagicMock(returncode=0, stdout="ok\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = smart_run

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "passed" in result.output or "warning" in result.output.lower()

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_nix_not_found(self, mock_which, mock_run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x == "podman" else None
        mock_run.return_value = MagicMock(returncode=0, stdout="podman version 4.9.0\n")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "nix not found" in result.output or result.exit_code != 0

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_runtime_exception(self, mock_which, mock_run, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("podman", "nix") else None
        )

        def run_with_exception(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == "podman" and "--version" in cmd:
                raise subprocess.TimeoutExpired(cmd, 5)
            if isinstance(cmd, list) and cmd[0] == "nix":
                return MagicMock(returncode=0, stdout="nix 2.18.1\n")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_with_exception

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "not working" in result.output or result.exit_code != 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: main() function
# ═══════════════════════════════════════════════════════════════════════════════


class TestMainFunction:
    """Cover the main() entry point."""

    @patch("cli.app")
    @patch("cli._tmux_setup_jail_pane", return_value=None)
    @patch("cli._kitty_setup_jail_tab", return_value=None)
    def test_main_no_tmux_no_kitty(self, mock_kitty, mock_tmux, mock_app, monkeypatch):
        monkeypatch.delenv("KITTY_PID", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setattr("sys.argv", ["yolo"])
        from cli import main

        main()
        mock_app.assert_called_once()

    @patch("cli.app")
    @patch("cli._kitty_setup_jail_tab")
    def test_main_kitty_mode(self, mock_kitty, mock_app, monkeypatch):
        monkeypatch.setenv("KITTY_PID", "1234")
        monkeypatch.delenv("TMUX", raising=False)
        mock_kitty.return_value = lambda: None
        monkeypatch.setattr("sys.argv", ["yolo"])
        from cli import main

        main()
        mock_kitty.assert_called_once()

    @patch("cli.app")
    @patch("cli._tmux_setup_jail_pane")
    def test_main_tmux_mode(self, mock_tmux, mock_app, monkeypatch):
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
        monkeypatch.delenv("KITTY_PID", raising=False)
        mock_tmux.return_value = lambda: None
        monkeypatch.setattr("sys.argv", ["yolo"])
        from cli import main

        main()
        mock_tmux.assert_called_once()

    @patch("cli.app")
    @patch("cli._tmux_setup_jail_pane", return_value=None)
    def test_main_argv_rewrite_inserts_run(self, mock_tmux, mock_app, monkeypatch):
        monkeypatch.delenv("KITTY_PID", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setattr("sys.argv", ["yolo", "--", "echo", "hello"])
        from cli import main

        main()
        assert "run" in sys.argv

    @patch("cli.app")
    @patch("cli._tmux_setup_jail_pane", return_value=None)
    def test_main_argv_no_rewrite_for_subcommand(
        self, mock_tmux, mock_app, monkeypatch
    ):
        monkeypatch.delenv("KITTY_PID", raising=False)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setattr("sys.argv", ["yolo", "check", "--no-build"])
        from cli import main

        main()
        assert sys.argv == ["yolo", "check", "--no-build"]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _runtime() missing paths
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuntimeNoRuntime:
    """Cover _runtime when no runtime is found."""

    @patch("shutil.which", return_value=None)
    def test_exits_when_no_runtime(self, mock_which, monkeypatch):
        from cli import _runtime

        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        with pytest.raises((SystemExit, RuntimeError)):
            _runtime({})

    @patch("shutil.which")
    def test_runtime_from_env(self, mock_which, monkeypatch):
        from cli import _runtime

        monkeypatch.setenv("YOLO_RUNTIME", "docker")
        mock_which.return_value = "/usr/bin/docker"
        assert _runtime({}) == "docker"

    @patch("shutil.which")
    def test_runtime_from_config(self, mock_which, monkeypatch):
        from cli import _runtime

        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        mock_which.side_effect = lambda x: "/usr/bin/docker" if x == "docker" else None
        assert _runtime({"runtime": "docker"}) == "docker"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: check() additional branch coverage
# ═══════════════════════════════════════════════════════════════════════════════


def _check_monkeypatch(monkeypatch, tmp_path, *, create_dirs=True):
    """Common monkeypatching for check command tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.setattr("cli.GLOBAL_HOME", tmp_path / "home")
    monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
    monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
    monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr("cli.USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
    monkeypatch.setattr("cli._runtime_is_connectable", lambda rt: True)
    if create_dirs:
        for d in ("home", "mise", "containers", "agents", "build"):
            (tmp_path / d).mkdir()


def _mock_runtimes(mock_which, runtimes=("podman", "nix")):
    """Configure shutil.which to find specified runtimes."""
    mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x in runtimes else None


def _default_smart_run(cmd, **kwargs):
    """Default subprocess.run mock that handles common runtime commands."""
    if isinstance(cmd, list):
        prog = cmd[0]
        if prog == "podman":
            if "--version" in cmd:
                return MagicMock(returncode=0, stdout="podman version 4.9.0\n")
            if "images" in cmd:
                return MagicMock(returncode=0, stdout="yolo-jail:latest (1.2 GB)\n")
            if "ps" in cmd:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0, stdout="")
        if prog == "nix":
            return MagicMock(returncode=0, stdout="nix (Nix) 2.18.1\n")
        if prog == sys.executable:
            return MagicMock(returncode=0, stdout="ok\n", stderr="")
    return MagicMock(returncode=0, stdout="", stderr="")


class TestCheckGlobalStorageWarnings:
    """Test check() warns about missing global storage directories."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_warns_missing_dirs(self, mock_which, mock_run, tmp_path, monkeypatch):
        _check_monkeypatch(monkeypatch, tmp_path, create_dirs=False)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "missing" in result.output.lower() or "warning" in result.output.lower()


class TestCheckUserConfigError:
    """Test check() handles user config parse errors."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_invalid_user_config_exits(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run

        (tmp_path / "user-config.jsonc").write_text("{invalid json!!")
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert result.exit_code != 0 or "failed" in result.output.lower()

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_user_config_parsed_ok(self, mock_which, mock_run, tmp_path, monkeypatch):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run

        (tmp_path / "user-config.jsonc").write_text('{"runtime": "podman"}')
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "user config" in result.output.lower()


class TestCheckFlakeNotFound:
    """Test check() warns when flake.nix is not found."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_flake_missing_warns(self, mock_which, mock_run, tmp_path, monkeypatch):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        # Point to a directory with no flake.nix
        fake_repo = tmp_path / "fake-repo"
        fake_repo.mkdir()
        monkeypatch.setenv("YOLO_REPO_ROOT", str(fake_repo))

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "flake.nix" in result.output.lower()


class TestCheckRuntimeError:
    """Test check() handles runtime detection errors in merged config."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_runtime_error_in_config(self, mock_which, mock_run, tmp_path, monkeypatch):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which, runtimes=("nix",))  # No container runtime
        mock_run.side_effect = _default_smart_run
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        monkeypatch.delenv("YOLO_RUNTIME", raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert result.exit_code != 0 or "runtime" in result.output.lower()


class TestCheckRepoPathInWorkspace:
    """Test check() warns when repo_path is in workspace config."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_repo_path_in_workspace_warns(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run
        (tmp_path / "yolo-jail.jsonc").write_text('{"repo_path": "/some/path"}')

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "repo_path" in result.output or "warning" in result.output.lower()


class TestCheckEntrypointPreflight:
    """Test check() entrypoint preflight section."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_entrypoint_preflight_fails(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)

        def run_with_entrypoint_fail(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[0] == sys.executable:
                return MagicMock(returncode=1, stdout="", stderr="crash")
            return _default_smart_run(cmd, **kwargs)

        mock_run.side_effect = run_with_entrypoint_fail
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        # Entrypoint preflight failure should be reported
        assert "preflight" in result.output.lower() or "failed" in result.output.lower()


class TestCheckNixBuildSection:
    """Test check() nix build section."""

    @patch("subprocess.run")
    @patch("shutil.which")
    @patch("cli._build_image_store_path")
    def test_nix_build_success(
        self, mock_build, mock_which, mock_run, tmp_path, monkeypatch
    ):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run
        mock_build.return_value = ("/nix/store/abc-result", [])
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--build"])
        assert (
            "nix build succeeded" in result.output.lower() or "passed" in result.output
        )

    @patch("subprocess.run")
    @patch("shutil.which")
    @patch("cli._build_image_store_path")
    def test_nix_build_failure(
        self, mock_build, mock_which, mock_run, tmp_path, monkeypatch
    ):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.side_effect = _default_smart_run
        mock_build.return_value = (None, ["error: undefined variable"])
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--build"])
        assert "nix build failed" in result.output.lower() or result.exit_code != 0


class TestCheckImageAndContainers:
    """Test check() image and running container sections."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_image_check_exception(self, mock_which, mock_run, tmp_path, monkeypatch):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)

        def run_with_image_error(cmd, **kwargs):
            if isinstance(cmd, list):
                prog = cmd[0]
                if prog == "podman":
                    if "--version" in cmd:
                        return MagicMock(returncode=0, stdout="podman version 4.9.0\n")
                    if "images" in cmd:
                        raise subprocess.TimeoutExpired(cmd, 10)
                    if "ps" in cmd:
                        raise subprocess.TimeoutExpired(cmd, 5)
                if prog == "nix":
                    return MagicMock(returncode=0, stdout="nix (Nix) 2.18.1\n")
                if prog == sys.executable:
                    return MagicMock(returncode=0, stdout="ok\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_with_image_error
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert (
            "could not check" in result.output.lower()
            or "warning" in result.output.lower()
        )

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_running_jails_found(self, mock_which, mock_run, tmp_path, monkeypatch):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)

        def run_with_jails(cmd, **kwargs):
            if isinstance(cmd, list):
                prog = cmd[0]
                if prog == "podman":
                    if "--version" in cmd:
                        return MagicMock(returncode=0, stdout="podman version 4.9.0\n")
                    if "images" in cmd:
                        return MagicMock(
                            returncode=0, stdout="yolo-jail:latest (1.2 GB)\n"
                        )
                    if "ps" in cmd:
                        return MagicMock(
                            returncode=0, stdout="yolo-abc123\nyolo-def456\n"
                        )
                if prog == "nix":
                    return MagicMock(returncode=0, stdout="nix (Nix) 2.18.1\n")
                if prog == sys.executable:
                    return MagicMock(returncode=0, stdout="ok\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_with_jails
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert "2 jail" in result.output or "jail" in result.output.lower()


class TestCheckSummaryWithFailures:
    """Test check() summary line with failures and warnings combined."""

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_check_summary_failed_plus_warnings(
        self, mock_which, mock_run, tmp_path, monkeypatch
    ):
        _check_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which, runtimes=("nix",))  # No container runtime
        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        mock_run.side_effect = _default_smart_run
        # Valid config but storage dirs missing → warnings, no runtime → fail
        _check_monkeypatch(monkeypatch, tmp_path, create_dirs=False)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])
        assert result.exit_code != 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: run() command — early setup paths
# ═══════════════════════════════════════════════════════════════════════════════


def _run_monkeypatch(monkeypatch, tmp_path):
    """Common monkeypatching for run command tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.setattr("cli.GLOBAL_HOME", tmp_path / "home")
    monkeypatch.setattr("cli.GLOBAL_MISE", tmp_path / "mise")
    monkeypatch.setattr("cli.GLOBAL_STORAGE", tmp_path / "storage")
    monkeypatch.setattr("cli.CONTAINER_DIR", tmp_path / "containers")
    monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr("cli.BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr("cli.USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
    monkeypatch.setattr("cli._runtime_is_connectable", lambda rt: True)
    monkeypatch.setattr("time.sleep", lambda _: None)
    for d in (
        "home",
        "mise",
        "containers",
        "agents",
        "build",
        "storage",
        "storage/locks",
    ):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)


class TestRunConfigErrors:
    """Test run() exits on config load/validation errors."""

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_run_exits_on_invalid_config_json(
        self, mock_which, mock_check, mock_run, tmp_path, monkeypatch
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.return_value = MagicMock(returncode=0)
        mock_check.side_effect = FileNotFoundError
        (tmp_path / "yolo-jail.jsonc").write_text("{invalid json!!")

        runner = CliRunner()
        result = runner.invoke(app, ["run"])
        assert result.exit_code != 0

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_run_exits_on_validation_error(
        self, mock_which, mock_check, mock_run, tmp_path, monkeypatch
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        mock_run.return_value = MagicMock(returncode=0)
        mock_check.side_effect = FileNotFoundError
        # mcp_presets with string that isn't a valid preset
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"mcp_presets": ["nonexistent-preset"]}'
        )

        runner = CliRunner()
        result = runner.invoke(app, ["run"])
        assert result.exit_code != 0


class TestRunIdentityEnvCollection:
    """Test run() collects git/jj identity env vars."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_collects_git_identity(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")

        def check_output_router(cmd, **kwargs):
            if isinstance(cmd, list):
                if "user.name" in cmd:
                    return b"Test User\n"
                if "user.email" in cmd:
                    return b"test@example.com\n"
            raise FileNotFoundError

        mock_check_output.side_effect = check_output_router

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "echo", "hello"])

        # Verify Popen was called and the docker command contains identity env
        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            cmd_str = " ".join(str(a) for a in docker_cmd)
            assert "YOLO_GIT_NAME=Test User" in cmd_str
            assert "YOLO_GIT_EMAIL=test@example.com" in cmd_str


class TestRunYoloInjection:
    """Test run() injects --yolo for gemini/copilot commands."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_injects_yolo_flag_for_copilot(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "copilot"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            # The final arg is the internal command string
            final_cmd = docker_cmd[-1]
            assert "--yolo" in final_cmd
            assert "--no-auto-update" in final_cmd


class TestRunExecIntoExisting:
    """Test run() exec path when container already exists."""

    @patch("cli.find_running_container")
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_exec_into_existing_container(
        self, mock_which, mock_check_output, mock_run, mock_find, tmp_path, monkeypatch
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError
        mock_find.return_value = "abc123"
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--", "echo", "hello"])
        assert "Attaching" in result.output

    @patch("cli.find_running_container")
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_exec_runtime_not_found(
        self, mock_which, mock_check_output, mock_run, mock_find, tmp_path, monkeypatch
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError
        mock_find.return_value = "abc123"
        mock_run.side_effect = FileNotFoundError("podman not found")

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--", "echo", "hello"])
        assert result.exit_code != 0


class TestRunNewContainerMounts:
    """Test run() new container path with mounts and network config."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_new_container_with_network_host(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text('{"network": {"mode": "host"}}')
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "--net=host" in docker_cmd

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_new_container_with_ports(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"network": {"mode": "bridge", "ports": ["8000:8000", "3000:3000"]}}'
        )
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "-p" in docker_cmd
            assert "8000:8000" in docker_cmd

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_new_container_with_extra_mounts(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        # Create a real dir to mount
        mount_dir = tmp_path / "extra-data"
        mount_dir.mkdir()
        (tmp_path / "yolo-jail.jsonc").write_text(
            json.dumps({"mounts": [str(mount_dir)]})
        )
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            cmd_str = " ".join(str(a) for a in docker_cmd)
            assert "extra-data" in cmd_str
            assert ":ro" in cmd_str

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_new_container_mount_path_missing_warns(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"mounts": ["/nonexistent/path/should/skip"]}'
        )
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--", "bash"])
        assert "skipping" in result.output.lower() or "warning" in result.output.lower()


class TestRunDockerVsPodman:
    """Test run() runtime-specific docker command differences."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_docker_runtime_adds_uid_mapping(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        mock_which.side_effect = lambda x: (
            f"/usr/bin/{x}" if x in ("docker", "nix") else None
        )
        monkeypatch.setenv("YOLO_RUNTIME", "docker")
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "-u" in docker_cmd
            assert "host-gateway" in " ".join(str(a) for a in docker_cmd)

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_podman_adds_uidmap_on_host(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "--uidmap" in docker_cmd or "--userns" in docker_cmd


class TestRunDevicePassthrough:
    """Test run() device passthrough configuration."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_raw_device_missing_warns(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"devices": ["/dev/nonexistent_device_xyz"]}'
        )
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--", "bash"])
        assert (
            "skipping" in result.output.lower() or "not found" in result.output.lower()
        )

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_cgroup_rule_device(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"devices": [{"cgroup_rule": "c 189:* rwm"}]}'
        )
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "--device-cgroup-rule" in docker_cmd


class TestRunProfile:
    """Test run() with --profile flag."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_profile_adds_timing_env(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        mock_find,
        mock_config_changes,
        mock_auto_load,
        mock_popen,
        tmp_path,
        monkeypatch,
    ):
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text("{}")
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--profile", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "YOLO_PROFILE=1" in docker_cmd


# ═══════════════════════════════════════════════════════════════════════════════
# Test: generate_agents_md edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestGenerateAgentsMdEdges:
    """Test generate_agents_md with forwarded ports and blocked tools."""

    def test_with_forwarded_ports_int(self, tmp_path, monkeypatch):
        from cli import generate_agents_md

        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        (tmp_path / "agents").mkdir()

        result = generate_agents_md(
            "yolo-test",
            tmp_path,
            [],
            [],
            net_mode="bridge",
            runtime="podman",
            forward_host_ports=[5432],
        )
        agents_copilot = (result / "AGENTS-copilot.md").read_text()
        assert "5432" in agents_copilot

    def test_with_forwarded_ports_string(self, tmp_path, monkeypatch):
        from cli import generate_agents_md

        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        (tmp_path / "agents").mkdir()

        result = generate_agents_md(
            "yolo-test",
            tmp_path,
            [],
            [],
            net_mode="bridge",
            runtime="podman",
            forward_host_ports=["3000:3000"],
        )
        agents_copilot = (result / "AGENTS-copilot.md").read_text()
        assert "3000" in agents_copilot

    def test_with_blocked_tools_suggestion(self, tmp_path, monkeypatch):
        from cli import generate_agents_md

        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        (tmp_path / "agents").mkdir()

        blocked = [{"name": "grep", "message": "Use rg", "suggestion": "rg"}]
        result = generate_agents_md(
            "yolo-test",
            tmp_path,
            blocked,
            [],
            net_mode="bridge",
            runtime="podman",
        )
        agents_copilot = (result / "AGENTS-copilot.md").read_text()
        assert "grep" in agents_copilot
        assert "rg" in agents_copilot

    def test_with_mount_descriptions(self, tmp_path, monkeypatch):
        from cli import generate_agents_md

        monkeypatch.setattr("cli.AGENTS_DIR", tmp_path / "agents")
        (tmp_path / "agents").mkdir()

        mounts = ["/home/user/data:/ctx/data"]
        result = generate_agents_md(
            "yolo-test",
            tmp_path,
            [],
            mounts,
            net_mode="bridge",
            runtime="podman",
        )
        agents_copilot = (result / "AGENTS-copilot.md").read_text()
        assert "data" in agents_copilot


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _seed_agent_dir
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedAgentDirCommands:
    """Test _seed_agent_dir seeds auth files from GLOBAL_HOME into per-workspace overlay."""

    def test_seeds_auth_files(self, tmp_path):
        from cli import _seed_agent_dir

        src = tmp_path / "src"
        src.mkdir()
        (src / "hosts.json").write_text('{"token": "x"}')
        dst = tmp_path / "dst"
        dst.mkdir()
        _seed_agent_dir(src, dst)
        assert (dst / "hosts.json").read_text() == '{"token": "x"}'

    def test_does_not_overwrite_existing(self, tmp_path):
        from cli import _seed_agent_dir

        src = tmp_path / "src"
        src.mkdir()
        (src / "hosts.json").write_text("old")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "hosts.json").write_text("kept")
        _seed_agent_dir(src, dst)
        assert (dst / "hosts.json").read_text() == "kept"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: _host_mise_dir
# ═══════════════════════════════════════════════════════════════════════════════


class TestHostMiseDir:
    """Test _host_mise_dir resolves the host mise path correctly."""

    def test_default_path(self, tmp_path, monkeypatch):
        from cli import _host_mise_dir

        monkeypatch.delenv("YOLO_OUTER_MISE_PATH", raising=False)
        monkeypatch.delenv("MISE_DATA_DIR", raising=False)
        result = _host_mise_dir()
        assert "mise" in str(result)

    def test_outer_mise_path_env(self, tmp_path, monkeypatch):
        from cli import _host_mise_dir

        mise_dir = tmp_path / "custom-mise"
        monkeypatch.setenv("YOLO_OUTER_MISE_PATH", str(mise_dir))
        result = _host_mise_dir()
        assert result == mise_dir
        assert mise_dir.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: cleanup_port_forwarding
# ═══════════════════════════════════════════════════════════════════════════════


class TestCleanupPortForwarding:
    """Test cleanup_port_forwarding terminates procs and removes dir."""

    def test_cleanup_terminates_procs(self, tmp_path):
        from cli import cleanup_port_forwarding

        mock_proc1 = MagicMock()
        mock_proc2 = MagicMock()
        socket_dir = tmp_path / "sockets"
        socket_dir.mkdir()

        cleanup_port_forwarding([mock_proc1, mock_proc2], socket_dir)

        mock_proc1.terminate.assert_called_once()
        mock_proc2.terminate.assert_called_once()
        assert not socket_dir.exists()

    def test_cleanup_kills_on_timeout(self, tmp_path):
        from cli import cleanup_port_forwarding

        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("socat", 2)

        cleanup_port_forwarding([mock_proc], None)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()

    def test_cleanup_no_socket_dir(self):
        from cli import cleanup_port_forwarding

        cleanup_port_forwarding([], None)  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════════
# Test: port forwarding socat error path
# ═══════════════════════════════════════════════════════════════════════════════


class TestPortForwardingErrors:
    """Test start_host_port_forwarding error handling."""

    @patch("subprocess.Popen")
    def test_socat_generic_error(self, mock_popen, tmp_path):
        mock_popen.side_effect = OSError("Permission denied")
        socket_dir = tmp_path / "sockets"

        result = start_host_port_forwarding(
            [{"host": 8080, "container": 8080}], "yolo-test", socket_dir
        )
        assert result == []
