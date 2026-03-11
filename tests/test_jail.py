import os
import subprocess
import json
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()

pytestmark = pytest.mark.slow


@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary project directory with a yolo-jail.jsonc."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    config = {
        "security": {
            "blocked_tools": [
                "curl",
                {"name": "grep", "message": "NO GREP ALLOWED", "suggestion": "use rg"},
            ]
        },
        "network": {"mode": "bridge"},
    }

    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)

    return project_dir


def run_yolo(project_dir, command, timeout=120):
    """Run a shell command inside the jail via login shell (bash -lc)."""
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
        timeout=timeout,
        env={**os.environ, "TERM": "dumb"},
    )
    return result


def run_yolo_direct(project_dir, *args, timeout=120):
    """Run a command directly via `yolo -- <cmd>`, matching real-world usage.

    This mirrors `yolo -- copilot --version` exactly — the command is NOT
    wrapped in bash -lc, so it exercises the non-login PATH setup in the
    entrypoint (the path that caused `copilot: command not found`).
    """
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
            *args,
        ],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "TERM": "dumb"},
    )
    return result


def test_blocked_tool_curl(temp_project):
    """Test that curl is blocked."""
    result = run_yolo(temp_project, "curl --version")
    assert result.returncode == 127
    assert "Error: tool curl is blocked" in result.stderr


def test_blocked_tool_grep(temp_project):
    """Test that grep is blocked."""
    result = run_yolo(temp_project, "grep 'foo' bar")
    assert result.returncode == 127
    assert "NO GREP ALLOWED" in result.stderr


def test_allowed_tool(temp_project):
    """Test that an allowed tool works."""
    result = run_yolo(temp_project, "ls -d /workspace")
    assert result.returncode == 0
    assert "/workspace" in result.stdout


def test_yolo_init(tmp_path):
    """Test yolo init command."""
    project_dir = tmp_path / "init_test"
    project_dir.mkdir()

    subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            "python",
            str(REPO_ROOT / "src" / "cli.py"),
        ]
        + ["init"],
        cwd=str(project_dir),
        check=True,
    )

    assert (project_dir / "yolo-jail.jsonc").exists()


def test_shim_persistence(tmp_path):
    """Test that shims don't persist after being removed from config."""
    project_dir = tmp_path / "persistence_test"
    project_dir.mkdir()

    # 1. Block curl
    config = {"security": {"blocked_tools": ["curl"]}}
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)

    result = run_yolo(project_dir, "curl --version")
    assert result.returncode == 127

    # 2. Unblock curl
    config = {"security": {"blocked_tools": []}}
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)

    result = run_yolo(project_dir, "curl --version")
    assert result.returncode == 0


def test_agent_tools_available(tmp_path):
    """Test that gemini and copilot are available inside the jail."""
    project_dir = tmp_path / "agent_test"
    project_dir.mkdir()
    result = run_yolo(project_dir, "gemini --version && copilot --version")
    assert result.returncode == 0


def test_agent_tools_available_direct(tmp_path):
    """Test that copilot/gemini work when invoked directly (not via bash -lc).

    This is the exact path taken by `yolo -- copilot`, which previously
    failed with 'copilot: command not found' because /mise/shims was absent
    from the non-login-shell PATH.
    """
    project_dir = tmp_path / "direct_agent_test"
    project_dir.mkdir()
    result = run_yolo_direct(project_dir, "copilot", "--version")
    assert result.returncode == 0, (
        f"copilot --version failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_yolo_direct_command(tmp_path):
    """Test running a direct command like 'yolo -- ls'."""
    project_dir = tmp_path / "direct_cmd_test"
    project_dir.mkdir()

    # Run yolo with the explicit -- delimiter
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            "python",
            str(REPO_ROOT / "src" / "cli.py"),
        ]
        + ["run", "--", "ls", "-d", "/workspace"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        env={**os.environ, "TERM": "dumb"},
    )
    assert result.returncode == 0
    assert "/workspace" in result.stdout


def test_jail_configs_present(temp_project):
    """Test that the persistent jail configs (YOLO mode) are visible."""
    # We check if the files we just created in the global storage are visible inside
    result = run_yolo(
        temp_project,
        "ls /home/agent/.copilot/config.json && ls /home/agent/.gemini/settings.json",
    )
    assert result.returncode == 0


def test_workspace_agents_untouched_and_home_agents_present(temp_project):
    """Workspace AGENTS.md should remain untouched; AGENTS context should be in app home dirs."""
    workspace_agents = temp_project / "AGENTS.md"
    original = "project-owned agents file\n"
    workspace_agents.write_text(original)

    result = run_yolo(
        temp_project,
        "ls /home/agent/.copilot/AGENTS.md && ls /home/agent/.gemini/AGENTS.md",
    )
    assert result.returncode == 0
    assert workspace_agents.read_text() == original


def test_venv_symlinks_resolve(temp_project):
    """Test that host .venv python symlinks resolve inside the jail."""
    # Always use /mise as the base if available: it's mounted in all jails (inner and outer).
    # On the host, fall back to MISE_DATA_DIR or the default mise data dir.
    if Path("/mise/installs/python").exists():
        mise_base = Path("/mise")
    else:
        mise_base = Path(
            os.environ.get("MISE_DATA_DIR", str(Path.home() / ".local/share/mise"))
        )

    installs = mise_base / "installs" / "python"
    if not installs.exists():
        pytest.skip("No mise python installs found")

    versions = [
        d
        for d in installs.iterdir()
        if d.is_dir() and not d.is_symlink() and (d / "bin").exists()
    ]
    if not versions:
        pytest.skip("No mise python installs found")

    # Pick the version matching the running Python — guaranteed to exist in all jails.
    import sys

    running_ver = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    version_dir = None
    for v in versions:
        if v.name == running_ver:
            version_dir = v
            break
    if version_dir is None:
        # Fall back to last concrete version if exact match not found
        version_dir = sorted(versions)[-1]
    # Find a real python interpreter (not python*-config)
    python_bin = None
    for candidate in sorted(version_dir.glob("bin/python3.*")):
        if "-config" not in candidate.name:
            python_bin = candidate
            break
    if not python_bin:
        pytest.skip("No python binary in mise install")

    venv_dir = temp_project / ".venv" / "bin"
    venv_dir.mkdir(parents=True)
    (venv_dir / "python").symlink_to(python_bin)

    result = run_yolo(temp_project, "/workspace/.venv/bin/python --version")
    assert result.returncode == 0, (
        f"symlink target: {python_bin}, stderr: {result.stderr}"
    )
    assert "Python" in result.stdout
    assert "Python" in result.stdout


def test_mise_venv_activation(tmp_path):
    """Test that mise.toml with _.python.venv activates the venv automatically."""
    project_dir = tmp_path / "venv_test"
    project_dir.mkdir()

    with open(project_dir / "mise.toml", "w") as f:
        f.write(
            '[tools]\npython = "3"\n\n[env]\n_.python.venv = { path = ".venv", create = true }\n'
        )

    # Longer timeout: mise may need to download+install python on first run
    result = run_yolo(project_dir, "echo $VIRTUAL_ENV", timeout=300)
    assert result.returncode == 0
    assert ".venv" in result.stdout


def test_vscode_mcp_shadowed(temp_project):
    """Test that workspace .vscode/mcp.json is shadowed with /dev/null inside jail."""
    vscode_dir = temp_project / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "mcp.json").write_text('{"servers": {"bad": {"command": "false"}}}')

    result = run_yolo(temp_project, "cat /workspace/.vscode/mcp.json")
    # /dev/null is empty, so cat should output nothing
    assert result.stdout.strip() == ""


def test_overmind_socket_isolated(temp_project):
    """Test that OVERMIND_SOCKET points outside /workspace so host/jail don't conflict."""
    result = run_yolo(temp_project, "echo $OVERMIND_SOCKET")
    socket_path = result.stdout.strip()
    assert socket_path, "OVERMIND_SOCKET should be set"
    assert not socket_path.startswith("/workspace"), (
        f"OVERMIND_SOCKET must not be inside /workspace (got {socket_path})"
    )


def test_overmind_host_sock_not_visible(temp_project):
    """Test that a host-side .overmind.sock is not visible inside the jail."""
    # Create a fake .overmind.sock file in the workspace (simulates host overmind)
    sock_file = temp_project / ".overmind.sock"
    sock_file.write_text("fake-host-socket")

    result = run_yolo(temp_project, "cat /workspace/.overmind.sock 2>&1; echo EXIT=$?")
    output = result.stdout.strip()
    # The file should either not exist or be empty (shadowed)
    assert "fake-host-socket" not in output, (
        f"Host .overmind.sock leaked into jail: {output}"
    )
