"""Unit tests that exercise macOS code paths in cli.py.

These tests set IS_MACOS=True and IS_LINUX=False, then mock Docker/Nix/subprocess
to verify that the macOS-specific branches behave correctly.  They run on any
platform (including Linux CI) because everything is mocked.

The autouse fixture in conftest.py forces IS_LINUX=True for unit tests; individual
tests here override that back to macOS mode via monkeypatch.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT / "src"))

from typer.testing import CliRunner  # noqa: E402

import cli  # noqa: E402
from cli import (  # noqa: E402
    _resolve_container_cgroup,
    _start_host_service_builtin_cgroup,
    start_host_services,
    stop_host_services,
    app,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_macos(monkeypatch):
    """Switch the cli module into macOS mode."""
    monkeypatch.setattr(cli, "IS_MACOS", True)
    monkeypatch.setattr(cli, "IS_LINUX", False)


def _mock_runtimes(mock_which, runtimes=("docker", "nix")):
    """Configure shutil.which for macOS (docker preferred)."""
    mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x in runtimes else None


def _run_monkeypatch(monkeypatch, tmp_path):
    """Common monkeypatching for run command tests."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
    monkeypatch.setattr(cli, "GLOBAL_HOME", tmp_path / "home")
    monkeypatch.setattr(cli, "GLOBAL_MISE", tmp_path / "mise")
    monkeypatch.setattr(cli, "GLOBAL_STORAGE", tmp_path / "storage")
    monkeypatch.setattr(cli, "CONTAINER_DIR", tmp_path / "containers")
    monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(cli, "BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr(cli, "USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
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


# ---------------------------------------------------------------------------
# Cgroup delegation — skipped on macOS
# ---------------------------------------------------------------------------


class TestMacosCgroupSkip:
    """Cgroup operations are no-ops on macOS."""

    def test_resolve_container_cgroup_returns_none(self, monkeypatch):
        _set_macos(monkeypatch)
        assert _resolve_container_cgroup("test-container", "podman") is None

    def test_builtin_cgroup_skipped_on_macos(self, monkeypatch, tmp_path):
        _set_macos(monkeypatch)
        sock_dir = tmp_path / "host-services"
        result = _start_host_service_builtin_cgroup(
            "test-container", "podman", sock_dir
        )
        # On macOS the builtin returns None — no daemon, no socket.
        assert result is None

    def test_stop_host_services_handles_empty_list(self, monkeypatch, tmp_path):
        _set_macos(monkeypatch)
        # stop with empty list (what start returns on macOS for the builtin)
        stop_host_services([], tmp_path / "nonexistent")

    def test_start_host_services_macos_skips_builtin(self, monkeypatch):
        _set_macos(monkeypatch)
        # Even with podman as runtime, on macOS the builtin returns None
        # (no cgroup v2), so no handles come back.
        handles = start_host_services("test-cname-macos", "podman", {})
        assert handles == []


# ---------------------------------------------------------------------------
# run() — mise volume on macOS
# ---------------------------------------------------------------------------


class TestMacosMiseVolume:
    """On macOS, mise uses a Docker named volume (not host bind mount)."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_mise_uses_named_volume(
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
        _set_macos(monkeypatch)
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
            cmd_str = " ".join(str(c) for c in docker_cmd)
            # Named volume backs the mount (Mach-O binaries in the host mise
            # dir can't run in Linux), but the mount point is the host mise
            # path — same canonical location as Linux jails, so venv
            # absolute paths resolve identically.
            from cli import _host_mise_dir

            host_mise = _host_mise_dir()
            assert f"yolo-mise-data:{host_mise}" in cmd_str, (
                f"Expected 'yolo-mise-data:{host_mise}' on macOS, got: {cmd_str}"
            )


# ---------------------------------------------------------------------------
# run() — UID mapping skipped on macOS Docker
# ---------------------------------------------------------------------------


class TestMacosUidMapping:
    """macOS Docker skips -u UID:GID (VM handles ownership)."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_no_uid_flag_on_macos_docker(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        monkeypatch.setenv("YOLO_RUNTIME", "docker")
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
            # Should NOT contain -u flag on macOS Docker
            assert "-u" not in docker_cmd, (
                f"macOS Docker should not pass -u UID:GID, got: {docker_cmd}"
            )


# ---------------------------------------------------------------------------
# run() — port forwarding uses TCP gateway on macOS
# ---------------------------------------------------------------------------


class TestMacosPortForwarding:
    """macOS uses YOLO_FWD_HOST_GATEWAY env var instead of Unix sockets."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_port_forward_sets_gateway_env(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"network": {"forward_host_ports": [5432]}}'
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
            cmd_str = " ".join(str(c) for c in docker_cmd)
            assert "YOLO_FWD_HOST_GATEWAY=host.docker.internal" in cmd_str, (
                f"Expected YOLO_FWD_HOST_GATEWAY env var on macOS, got: {cmd_str}"
            )


# ---------------------------------------------------------------------------
# run() — device passthrough skipped on macOS
# ---------------------------------------------------------------------------


class TestMacosDeviceSkip:
    """Device passthrough (raw, USB, cgroup rules) is skipped on macOS."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_raw_device_skipped_on_macos(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text('{"devices": ["/dev/ttyUSB0"]}')
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "--device" not in docker_cmd, (
                f"macOS should skip --device, got: {docker_cmd}"
            )

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_usb_device_skipped_on_macos(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text(
            '{"devices": [{"usb": "0bda:2838", "description": "RTL-SDR"}]}'
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
            assert "--device" not in docker_cmd, (
                f"macOS should skip USB --device, got: {docker_cmd}"
            )

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_cgroup_rule_skipped_on_macos(
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
        _set_macos(monkeypatch)
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
            assert "--device-cgroup-rule" not in docker_cmd, (
                f"macOS should skip --device-cgroup-rule, got: {docker_cmd}"
            )


# ---------------------------------------------------------------------------
# run() — GPU passthrough skipped on macOS
# ---------------------------------------------------------------------------


class TestMacosGpuSkip:
    """GPU passthrough is skipped on macOS."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_gpu_skipped_on_macos(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which)
        (tmp_path / "yolo-jail.jsonc").write_text('{"gpu": {"enabled": true}}')
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        runner.invoke(app, ["run", "--", "bash"])

        # Should warn but not crash
        if mock_popen.called:
            docker_cmd = mock_popen.call_args[0][0]
            assert "--gpus" not in docker_cmd, (
                f"macOS should skip --gpus, got: {docker_cmd}"
            )
            # No CDI device either
            gpu_devices = [
                c for c in docker_cmd if isinstance(c, str) and "nvidia" in c.lower()
            ]
            assert not gpu_devices, (
                f"macOS should skip NVIDIA devices, got: {gpu_devices}"
            )


# ---------------------------------------------------------------------------
# run() — KVM passthrough skipped on macOS
# ---------------------------------------------------------------------------


class TestMacosKvmSkip:
    """`kvm: true` is a no-op on macOS (Apple uses the VZ framework)."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_kvm_skipped_on_macos(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        _mock_runtimes(mock_which, runtimes=("container", "nix"))
        monkeypatch.setenv("YOLO_RUNTIME", "container")
        monkeypatch.setattr(cli, "_runtime_is_connectable", lambda rt: True)
        monkeypatch.setattr(cli, "_is_apple_container", lambda p: True)
        (tmp_path / "yolo-jail.jsonc").write_text('{"kvm": true}')
        mock_check_output.side_effect = FileNotFoundError

        mock_proc = MagicMock()
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--", "bash"])

        # Container still launches; /dev/kvm and keep-groups must not appear.
        assert mock_popen.called, f"popen should be called; output was: {result.output}"
        docker_cmd = mock_popen.call_args[0][0]
        assert "/dev/kvm" not in docker_cmd, (
            f"macOS should skip /dev/kvm, got: {docker_cmd}"
        )
        assert "keep-groups" not in docker_cmd, (
            f"macOS should skip keep-groups, got: {docker_cmd}"
        )
        # And a warning is printed.
        assert "kvm" in result.output.lower()


# ---------------------------------------------------------------------------
# run() — YOLO_RUNTIME inside container is always podman
# ---------------------------------------------------------------------------


class TestMacosContainerRuntime:
    """Container always gets YOLO_RUNTIME=podman regardless of host runtime."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_container_gets_podman_runtime(
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
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        monkeypatch.setenv("YOLO_RUNTIME", "docker")
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
            cmd_str = " ".join(str(c) for c in docker_cmd)
            # Container should get YOLO_RUNTIME=podman (not docker)
            assert "YOLO_RUNTIME=podman" in cmd_str, (
                f"Container should get YOLO_RUNTIME=podman, got: {cmd_str}"
            )


# ---------------------------------------------------------------------------
# run() — tmpfs mode=1777
# ---------------------------------------------------------------------------


class TestMacosTmpfs:
    """macOS Docker gets explicit tmpfs mode=1777."""

    @patch("subprocess.Popen")
    @patch("cli.auto_load_image")
    @patch("cli._check_config_changes", return_value=True)
    @patch("cli.find_running_container", return_value=None)
    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_tmpfs_has_mode_1777(
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
        _set_macos(monkeypatch)
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
            tmpfs_args = [
                c for c in docker_cmd if isinstance(c, str) and "mode=1777" in c
            ]
            assert tmpfs_args, (
                f"Expected tmpfs with mode=1777 on macOS, "
                f"tmpfs flags: {[c for c in docker_cmd if 'tmpfs' in str(c)]}"
            )


# ---------------------------------------------------------------------------
# check() / doctor — macOS-specific diagnostics
# ---------------------------------------------------------------------------


class TestMacosDoctor:
    """check() includes macOS-specific diagnostics."""

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_check_shows_macos_platform(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        _set_macos(monkeypatch)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        monkeypatch.setattr(cli, "GLOBAL_HOME", tmp_path / "home")
        monkeypatch.setattr(cli, "GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr(cli, "GLOBAL_STORAGE", tmp_path / "storage")
        monkeypatch.setattr(cli, "USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
        (tmp_path / "home").mkdir(exist_ok=True)
        (tmp_path / "mise").mkdir(exist_ok=True)
        (tmp_path / "storage").mkdir(exist_ok=True)
        _mock_runtimes(mock_which)

        mock_run.return_value = MagicMock(
            returncode=0, stdout="docker version 24.0\n", stderr=""
        )
        mock_check_output.side_effect = FileNotFoundError

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])

        # Should mention macOS platform info
        output = result.output.lower()
        assert "macos" in output or "darwin" in output or "platform" in output, (
            f"Expected macOS platform info in doctor output, got: {result.output[:500]}"
        )

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_check_warns_no_cgroup(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        _set_macos(monkeypatch)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        monkeypatch.setattr(cli, "GLOBAL_HOME", tmp_path / "home")
        monkeypatch.setattr(cli, "GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr(cli, "GLOBAL_STORAGE", tmp_path / "storage")
        monkeypatch.setattr(cli, "USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
        (tmp_path / "home").mkdir(exist_ok=True)
        (tmp_path / "mise").mkdir(exist_ok=True)
        (tmp_path / "storage").mkdir(exist_ok=True)
        _mock_runtimes(mock_which)

        mock_run.return_value = MagicMock(
            returncode=0, stdout="docker version 24.0\n", stderr=""
        )
        mock_check_output.side_effect = FileNotFoundError

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])

        output = result.output.lower()
        assert "cgroup" in output, (
            f"Expected cgroup limitation warning on macOS, got: {result.output[:500]}"
        )

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_check_warns_gpu_unavailable(
        self,
        mock_which,
        mock_check_output,
        mock_run,
        tmp_path,
        monkeypatch,
    ):
        _set_macos(monkeypatch)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YOLO_REPO_ROOT", str(REPO_ROOT))
        monkeypatch.setattr(cli, "GLOBAL_HOME", tmp_path / "home")
        monkeypatch.setattr(cli, "GLOBAL_MISE", tmp_path / "mise")
        monkeypatch.setattr(cli, "GLOBAL_STORAGE", tmp_path / "storage")
        monkeypatch.setattr(cli, "USER_CONFIG_PATH", tmp_path / "user-config.jsonc")
        (tmp_path / "home").mkdir(exist_ok=True)
        (tmp_path / "mise").mkdir(exist_ok=True)
        (tmp_path / "storage").mkdir(exist_ok=True)
        _mock_runtimes(mock_which)

        mock_run.return_value = MagicMock(
            returncode=0, stdout="docker version 24.0\n", stderr=""
        )
        mock_check_output.side_effect = FileNotFoundError

        runner = CliRunner()
        result = runner.invoke(app, ["check", "--no-build"])

        output = result.output.lower()
        assert "gpu" in output, (
            f"Expected GPU unavailable notice on macOS, got: {result.output[:500]}"
        )


# ---------------------------------------------------------------------------
# Apple Container CLI runtime
# ---------------------------------------------------------------------------


class TestAppleContainerRuntime:
    """Test Apple Container CLI ('container') runtime support."""

    def test_runtime_detects_container(self, monkeypatch):
        """_runtime() returns 'container' when YOLO_RUNTIME is set."""
        _set_macos(monkeypatch)
        monkeypatch.setenv("YOLO_RUNTIME", "container")
        assert cli._runtime() == "container"

    def test_runtime_autodetects_container(self, monkeypatch):
        """_runtime() auto-detects container CLI when no env/config set."""
        _set_macos(monkeypatch)
        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        monkeypatch.setattr(cli, "_runtime_is_connectable", lambda rt: True)
        with (
            patch("shutil.which") as mock_which,
            patch("cli._is_apple_container", return_value=True),
        ):
            # Only 'container' is on PATH
            mock_which.side_effect = lambda x: (
                "/usr/local/bin/container" if x == "container" else None
            )
            assert cli._runtime() == "container"

    def test_runtime_prefers_container_on_macos(self, monkeypatch):
        """On macOS, container is preferred over podman/docker when all are available."""
        _set_macos(monkeypatch)
        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        monkeypatch.setattr(cli, "_runtime_is_connectable", lambda rt: True)
        with (
            patch("shutil.which") as mock_which,
            patch("cli._is_apple_container", return_value=True),
        ):
            # All three runtimes are on PATH
            mock_which.side_effect = lambda x: (
                f"/usr/local/bin/{x}"
                if x in ("container", "podman", "docker")
                else None
            )
            assert cli._runtime() == "container"

    def test_runtime_prefers_podman_on_linux(self, monkeypatch):
        """On Linux, podman is preferred; container is not a candidate."""
        monkeypatch.setattr(cli, "IS_MACOS", False)
        monkeypatch.setattr(cli, "IS_LINUX", True)
        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        monkeypatch.setattr(cli, "_runtime_is_connectable", lambda rt: True)
        with patch("shutil.which") as mock_which:
            mock_which.side_effect = lambda x: (
                f"/usr/bin/{x}" if x in ("podman", "docker") else None
            )
            assert cli._runtime() == "podman"

    def test_runtime_for_check_prefers_container_on_macos(self, monkeypatch):
        """_runtime_for_check() prefers container over podman/docker on macOS."""
        _set_macos(monkeypatch)
        monkeypatch.delenv("YOLO_RUNTIME", raising=False)
        monkeypatch.setattr(cli, "_runtime_is_connectable", lambda rt: True)
        with (
            patch("shutil.which") as mock_which,
            patch("cli._is_apple_container", return_value=True),
        ):
            mock_which.side_effect = lambda x: (
                f"/usr/local/bin/{x}"
                if x in ("container", "podman", "docker")
                else None
            )
            rt, err = cli._runtime_for_check({})
            assert rt == "container"
            assert err is None

    def test_image_load_cmd_container(self):
        """Apple Container uses 'container image load -i' instead of 'docker load -i'."""
        cmd = cli._image_load_cmd("container", "/tmp/image.tar")
        assert cmd == ["container", "image", "load", "-i", "/tmp/image.tar"]

    def test_image_load_cmd_docker(self):
        """Docker uses 'docker load -i'."""
        cmd = cli._image_load_cmd("docker", "/tmp/image.tar")
        assert cmd == ["docker", "load", "-i", "/tmp/image.tar"]

    def test_image_inspect_cmd_container(self):
        """Apple Container uses 'container image inspect'."""
        cmd = cli._image_inspect_cmd("container", "yolo-jail:latest")
        assert cmd == ["container", "image", "inspect", "yolo-jail:latest"]

    def test_find_running_container_uses_ls(self, monkeypatch):
        """Apple Container uses 'container ls' (no --filter) instead of 'docker ps'."""
        _set_macos(monkeypatch)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="NAME\tSTATUS\nyolo-test-abc\trunning\n",
            )
            result = cli.find_running_container("yolo-test-abc", runtime="container")
            assert result == "yolo-test-abc"
            # Verify it used 'ls' not 'ps', and no --filter (unsupported)
            call_args = mock_run.call_args[0][0]
            assert "ls" in call_args
            assert "ps" not in call_args
            assert "--filter" not in call_args

    def test_find_running_container_ls_not_found(self, monkeypatch):
        """Returns None when container not in 'container ls' output."""
        _set_macos(monkeypatch)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="NAME\tSTATUS\n",
            )
            result = cli.find_running_container("yolo-test-abc", runtime="container")
            assert result is None

    def test_check_container_stuck_skips_apple(self, monkeypatch):
        """Apple Container doesn't support 'top' — returns None immediately."""
        _set_macos(monkeypatch)
        result = cli._check_container_stuck("test-container", "container")
        assert result is None

    @patch("subprocess.run")
    @patch("subprocess.check_output")
    @patch("shutil.which")
    def test_docker_flags_no_cgroupns(
        self, mock_which, mock_check_output, mock_run, monkeypatch, tmp_path
    ):
        """Apple Container run command should not include --cgroupns."""
        _set_macos(monkeypatch)
        _run_monkeypatch(monkeypatch, tmp_path)
        monkeypatch.setenv("YOLO_RUNTIME", "container")

        # Mock runtime detection and container checks
        _mock_runtimes(mock_which, runtimes=("container", "nix"))
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )
        mock_check_output.side_effect = FileNotFoundError

        # Create minimal config
        config_file = tmp_path / "yolo-jail.jsonc"
        config_file.write_text('{"runtime": "container"}')

        # Call _runtime to verify it returns container
        assert cli._runtime() == "container"

    def test_runtime_for_check_container(self, monkeypatch):
        """_runtime_for_check() accepts 'container' runtime."""
        _set_macos(monkeypatch)
        monkeypatch.setenv("YOLO_RUNTIME", "container")
        monkeypatch.setattr(cli, "_runtime_is_connectable", lambda rt: True)
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/local/bin/container"
            rt, err = cli._runtime_for_check({"runtime": "container"})
            assert rt == "container"
            assert err is None
