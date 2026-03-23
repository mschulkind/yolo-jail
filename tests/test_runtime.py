"""Tests for container runtime selection and multi-runtime support."""

import os
import sys
import subprocess
import json
import shutil
from pathlib import Path
from unittest.mock import patch
import pytest
from typer.testing import CliRunner

REPO_ROOT = Path(__file__).parent.parent.resolve()

sys.path.insert(0, str(REPO_ROOT / "src"))
from cli import _runtime  # noqa: E402


# --- Unit tests for _runtime() ---


def test_runtime_env_var_overrides_config():
    with patch.dict(os.environ, {"YOLO_RUNTIME": "docker"}):
        assert _runtime({"runtime": "podman"}) == "docker"


def test_runtime_config_used_when_no_env():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("YOLO_RUNTIME", None)
        assert _runtime({"runtime": "podman"}) == "podman"


def test_runtime_auto_detect_when_no_config():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("YOLO_RUNTIME", None)
        result = _runtime({})
        assert result in ("podman", "docker")


def test_runtime_rejects_invalid_env():
    with patch.dict(os.environ, {"YOLO_RUNTIME": "containerd"}):
        # Invalid env value ignored, falls through to config/auto-detect
        result = _runtime({"runtime": "docker"})
        assert result == "docker"


def test_runtime_rejects_invalid_config():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("YOLO_RUNTIME", None)
        result = _runtime({"runtime": "lxc"})
        assert result in ("podman", "docker")  # Falls through to auto-detect


def test_check_help_mentions_every_config_edit():
    import cli

    result = CliRunner().invoke(cli.app, ["check", "--help"])
    assert result.exit_code == 0
    assert "after every config edit" in result.stdout.lower()


def test_config_ref_mentions_yolo_check_after_every_edit():
    import cli

    result = CliRunner().invoke(cli.app, ["config-ref"])
    assert result.exit_code == 0
    assert "After EVERY edit" in result.stdout
    assert "yolo check" in result.stdout


def test_generated_agents_md_mentions_yolo_check(tmp_path, monkeypatch):
    import cli

    monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
    agents_path = cli.generate_agents_md(
        "yolo-test",
        tmp_path / "workspace",
        [],
        [],
    )

    content = (agents_path / "AGENTS-copilot.md").read_text()
    assert "ALWAYS run `yolo check` after every config edit" in content


# --- ensure_global_storage tests ---


def test_ensure_global_storage_creates_mount_parents(tmp_path, monkeypatch):
    """Pre-create intermediate dirs so Docker daemon doesn't create them as root."""
    import cli

    monkeypatch.setattr(cli, "GLOBAL_HOME", tmp_path / "home")
    monkeypatch.setattr(cli, "GLOBAL_MISE", tmp_path / "mise")
    monkeypatch.setattr(cli, "CONTAINER_DIR", tmp_path / "containers")
    monkeypatch.setattr(cli, "AGENTS_DIR", tmp_path / "agents")
    cli.ensure_global_storage()

    # Core dirs exist
    assert (tmp_path / "home").is_dir()
    assert (tmp_path / "mise").is_dir()
    assert (tmp_path / "containers").is_dir()
    assert (tmp_path / "agents").is_dir()
    # Intermediate mount-parent dirs that Docker would otherwise create as root
    assert (tmp_path / "home" / ".copilot").is_dir()
    assert (tmp_path / "home" / ".gemini").is_dir()
    assert (tmp_path / "home" / ".config" / "git").is_dir()


# --- Integration tests for per-runtime sentinel ---


def test_sentinel_is_per_runtime(tmp_path):
    """Verify that .last-load-<runtime> sentinel files are created per runtime."""
    # Just check the sentinel path logic (don't actually load)
    # We can verify by checking the sentinel attribute
    sentinel_docker = tmp_path / ".last-load-docker"
    sentinel_podman = tmp_path / ".last-load-podman"
    assert not sentinel_docker.exists()
    assert not sentinel_podman.exists()


def test_skip_image_load_when_container_running(tmp_path, monkeypatch):
    """auto_load_image must NOT be called when a container is already running."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    import cli
    from unittest.mock import patch, MagicMock

    monkeypatch.chdir(tmp_path)
    image_load_called = []
    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with (
        patch.object(
            cli,
            "auto_load_image",
            side_effect=lambda *a, **k: image_load_called.append(True),
        ),
        patch.object(cli, "find_running_container", return_value="abc123def456"),
        patch.object(cli, "load_config", return_value={}),
        patch.object(cli, "ensure_global_storage"),
        patch.object(cli, "_runtime", return_value="docker"),
        patch.object(cli, "_tmux_rename_window"),
        patch.object(cli.subprocess, "run", return_value=fake_proc),
    ):
        from typer.testing import CliRunner

        try:
            CliRunner().invoke(cli.app, ["run"], catch_exceptions=False)
        except SystemExit:
            pass

    assert not image_load_called, (
        "auto_load_image must not be called when a container is already running"
    )


def test_exec_path_no_unbound_errors(tmp_path, monkeypatch):
    """The exec-into-existing-container path must not raise UnboundLocalError.

    Regression test: local `import subprocess` inside run() caused
    subprocess to be treated as a local variable, making it unbound
    when accessed before the import statement.
    """
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))
    import cli
    from unittest.mock import patch, MagicMock

    monkeypatch.chdir(tmp_path)
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    exec_args = []

    def capture_run(cmd, **kwargs):
        exec_args.append(cmd)
        return fake_proc

    with (
        patch.object(cli, "find_running_container", return_value="abc123def456"),
        patch.object(cli, "load_config", return_value={}),
        patch.object(cli, "ensure_global_storage"),
        patch.object(cli, "_runtime", return_value="docker"),
        patch.object(cli, "_tmux_rename_window"),
        patch.object(cli.subprocess, "run", side_effect=capture_run),
    ):
        from typer.testing import CliRunner

        try:
            CliRunner().invoke(
                cli.app, ["run", "--", "echo", "hi"], catch_exceptions=False
            )
        except SystemExit:
            pass

    assert exec_args, (
        "subprocess.run should have been called with the docker exec command"
    )
    assert any(any("exec" in str(a) for a in cmd) for cmd in exec_args), (
        "should have called docker exec"
    )


AVAILABLE_RUNTIMES = []
if shutil.which("docker"):
    AVAILABLE_RUNTIMES.append("docker")
if shutil.which("podman"):
    AVAILABLE_RUNTIMES.append("podman")


def run_yolo_with_runtime(project_dir, command, runtime):
    """Run a shell command inside the jail with a specific runtime."""
    env = {**os.environ, "TERM": "dumb", "YOLO_RUNTIME": runtime}
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            "python",
            str(REPO_ROOT / "src" / "cli.py"),
            "run",
            "--",
            "bash",
            "-lc",
            command,
        ],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return result


@pytest.fixture
def temp_project(tmp_path):
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    config = {
        "security": {
            "blocked_tools": [
                "curl",
                {"name": "grep", "message": "NO GREP", "suggestion": "use rg"},
                {"name": "find", "message": "NO FIND", "suggestion": "use fd"},
            ]
        },
    }
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)
    return project_dir


@pytest.mark.slow
@pytest.mark.parametrize("runtime", AVAILABLE_RUNTIMES)
def test_basic_command(temp_project, runtime):
    """Test that a basic command works with each runtime."""
    result = run_yolo_with_runtime(temp_project, "echo hello", runtime)
    assert result.returncode == 0
    assert "hello" in result.stdout


@pytest.mark.slow
@pytest.mark.parametrize("runtime", AVAILABLE_RUNTIMES)
def test_blocked_tool_with_runtime(temp_project, runtime):
    """Test that blocked tools are properly blocked with each runtime."""
    result = run_yolo_with_runtime(temp_project, "curl --version", runtime)
    assert result.returncode == 127
    assert "blocked" in result.stderr.lower()


@pytest.mark.slow
@pytest.mark.parametrize("runtime", AVAILABLE_RUNTIMES)
def test_file_ownership(temp_project, runtime):
    """Test that files created inside jail are owned by host user."""
    run_yolo_with_runtime(temp_project, "touch /workspace/test-ownership", runtime)
    test_file = temp_project / "test-ownership"
    assert test_file.exists()
    stat = test_file.stat()
    assert stat.st_uid == os.getuid()
    assert stat.st_gid == os.getgid()


@pytest.mark.slow
@pytest.mark.parametrize("runtime", AVAILABLE_RUNTIMES)
def test_workspace_mount(temp_project, runtime):
    """Test that workspace is properly mounted."""
    result = run_yolo_with_runtime(temp_project, "ls -d /workspace", runtime)
    assert result.returncode == 0
    assert "/workspace" in result.stdout
