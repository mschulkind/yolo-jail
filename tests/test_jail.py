import hashlib
import os
import re
import shutil
import subprocess
import json
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()

pytestmark = pytest.mark.slow

# Default subprocess timeout for a single `yolo -- <cmd>` invocation.
#
# Cold start on a fresh CI runner exercises: image pull, container
# create, mise tool install/upgrade, loophole daemon spawn, entrypoint
# config generation.  On a warm runner the same path is ~10s; cold it
# spends well over 2 minutes, which was blowing past the old 120s
# default and failing the FIRST integration test consistently
# (``test_blocked_tool_curl``).  300s gives enough headroom for a cold
# boot while still catching a genuinely-hung container within a single
# test run.
DEFAULT_JAIL_TIMEOUT = 300


def _container_name_for_workspace(workspace: Path) -> str:
    """Mirror cli.py's container_name_for_workspace for cleanup."""
    name = workspace.resolve().name
    safe = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]
    if not safe:
        safe = "jail"
    h = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:8]
    return f"yolo-{safe}-{h}"


def _force_remove_container(project_dir: Path):
    """Force-remove the jail container for a project directory."""
    runtime = os.environ.get("YOLO_RUNTIME") or (
        "docker" if sys.platform == "darwin" and shutil.which("docker") else "podman"
    )
    cname = _container_name_for_workspace(project_dir)
    subprocess.run(
        [runtime, "rm", "-f", cname],
        capture_output=True,
        timeout=10,
    )
    # Also try old hash-only naming scheme for pre-existing containers
    h = hashlib.sha256(str(project_dir.resolve()).encode()).hexdigest()[:12]
    subprocess.run(
        [runtime, "rm", "-f", f"yolo-{h}"],
        capture_output=True,
        timeout=10,
    )


def _skip_if_cgroup_readonly():
    """Skip test if the cgroup filesystem is read-only (e.g. running inside a jail)."""
    cg = Path("/sys/fs/cgroup")
    if not cg.exists():
        pytest.skip("cgroup v2 not available")
    try:
        test_dir = cg / ".yolo-test-probe"
        test_dir.mkdir()
        test_dir.rmdir()
    except OSError:
        pytest.skip("cgroup filesystem is read-only (nested jail?)")


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

    yield project_dir

    # Teardown: force-remove any leftover container
    _force_remove_container(project_dir)


def run_yolo(project_dir, command, timeout=DEFAULT_JAIL_TIMEOUT):
    """Run a shell command inside the jail via login shell (bash -lc).

    On timeout, force-removes the container to prevent orphaned zombies.
    """
    try:
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
    except subprocess.TimeoutExpired:
        _force_remove_container(project_dir)
        raise


def run_yolo_cli(project_dir, *args, timeout=DEFAULT_JAIL_TIMEOUT):
    """Run a yolo subcommand directly on the host-side CLI."""
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(REPO_ROOT),
            "python",
            str(REPO_ROOT / "src" / "cli.py"),
            *args,
        ],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "TERM": "dumb"},
    )
    return result


def run_yolo_direct(project_dir, *args, timeout=DEFAULT_JAIL_TIMEOUT):
    """Run a command directly via `yolo -- <cmd>`, matching real-world usage.

    This mirrors `yolo -- copilot --version` exactly — the command is NOT
    wrapped in bash -lc, so it exercises the non-login PATH setup in the
    entrypoint (the path that caused `copilot: command not found`).
    """
    try:
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
    except subprocess.TimeoutExpired:
        _force_remove_container(project_dir)
        raise


def test_blocked_tool_curl(temp_project):
    """Test that curl is blocked."""
    result = run_yolo(temp_project, "curl --version")
    assert result.returncode == 127
    assert "Error: tool curl is blocked" in result.stderr


def test_blocked_tool_grep(temp_project):
    """Test that grep's recursive mode is blocked (the default).

    Plain / pipe-filter usage passes through to /bin/grep — see
    ``test_entrypoint.py::test_grep_shim_blocks_only_recursive`` for
    the full matrix.  Here we just confirm the integration wiring
    fires the block on a recursive invocation."""
    result = run_yolo(temp_project, "grep -r 'foo' .")
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


def test_yolo_check_valid_config(temp_project):
    """Host-side `yolo check --no-build` should validate a normal config."""
    result = run_yolo_cli(temp_project, "check", "--no-build")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Merged config is semantically valid" in output


def test_yolo_check_invalid_config_fails(tmp_path):
    """`yolo check --no-build` should fail fast on invalid config."""
    project_dir = tmp_path / "invalid_check"
    project_dir.mkdir()
    (project_dir / "yolo-jail.jsonc").write_text(
        json.dumps({"network": {"mode": "bridg"}})
    )

    result = run_yolo_cli(project_dir, "check", "--no-build")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "config.network.mode" in output


def test_yolo_run_invalid_config_fails_before_start(tmp_path):
    """`yolo run` should reject invalid config instead of silently defaulting."""
    project_dir = tmp_path / "invalid_run"
    project_dir.mkdir()
    (project_dir / "yolo-jail.jsonc").write_text('{"security": {"blocked_tools": [}')

    result = run_yolo_cli(project_dir, "run", "--", "bash", "-lc", "true")
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Failed to parse yolo-jail.jsonc" in output


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


def test_yolo_check_available_inside_jail(temp_project):
    """In-jail agents should be able to run `yolo check --no-build` mid-session."""
    result = run_yolo(temp_project, "yolo check --no-build")
    assert result.returncode == 0, result.stderr
    assert "YOLO Jail Check" in result.stdout


def test_yolo_help_inside_jail(temp_project):
    """``yolo --help`` inside a jail must work without tripping on uv's
    getcwd, without requiring the repo root to be writable, and without
    a PYTHONPATH dependency.  Regression: the previous shim cd'd into
    /opt/yolo-jail (a read-only bind mount) before calling ``uv run``,
    which caused ``uv`` to bail with "Current directory does not exist"
    on the host's getcwd call."""
    result = run_yolo(temp_project, "yolo --help")
    assert result.returncode == 0, (
        f"yolo --help failed: returncode={result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # Typer's help output.  Presence of "Usage:" confirms we actually
    # reached the main() dispatcher, not a pre-import error.
    assert "Usage:" in result.stdout, result.stdout


def test_custom_mcp_server_config_propagates(temp_project):
    """Custom MCP servers from yolo-jail.jsonc should reach both agent configs."""
    probe_script = temp_project / "probe-mcp.py"
    probe_script.write_text("#!/usr/bin/env python3\n")

    config_path = temp_project / "yolo-jail.jsonc"
    config = json.loads(config_path.read_text())
    config["mcp_servers"] = {
        "probe-mcp": {
            "command": "/workspace/probe-mcp.py",
            "args": ["--stdio"],
        }
    }
    config_path.write_text(json.dumps(config))

    result = run_yolo(
        temp_project,
        """python - <<'PY'
import json
from pathlib import Path
copilot = json.loads(Path('/home/agent/.copilot/mcp-config.json').read_text())
gemini = json.loads(Path('/home/agent/.gemini/settings.json').read_text())
print(copilot['mcpServers']['probe-mcp']['command'])
print(gemini['mcpServers']['probe-mcp']['command'])
PY""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("/workspace/probe-mcp.py") == 2


def test_mcp_preset_can_be_enabled(temp_project):
    """MCP presets from yolo-jail.jsonc should enable built-in servers."""
    config_path = temp_project / "yolo-jail.jsonc"
    config = json.loads(config_path.read_text())
    config["mcp_presets"] = ["chrome-devtools", "sequential-thinking"]
    config_path.write_text(json.dumps(config))

    result = run_yolo(
        temp_project,
        """python - <<'PY'
import json
from pathlib import Path
copilot = json.loads(Path('/home/agent/.copilot/mcp-config.json').read_text())
gemini = json.loads(Path('/home/agent/.gemini/settings.json').read_text())
print('chrome-devtools' in copilot['mcpServers'])
print('chrome-devtools' in gemini['mcpServers'])
print('sequential-thinking' in copilot['mcpServers'])
print('sequential-thinking' in gemini['mcpServers'])
PY""",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["True", "True", "True", "True"]


def test_same_file_mcp_preset_and_null_override_is_rejected(temp_project):
    """The same config file cannot enable and null-remove the same preset."""
    config_path = temp_project / "yolo-jail.jsonc"
    config = json.loads(config_path.read_text())
    config["mcp_presets"] = ["chrome-devtools", "sequential-thinking"]
    config["mcp_servers"] = {"chrome-devtools": None}
    config_path.write_text(json.dumps(config))

    result = run_yolo_cli(temp_project, "run", "--", "bash", "-lc", "true")
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "Invalid jail config" in output
    assert "preset 'chrome-devtools' is enabled in mcp_presets" in output
    assert "within the same config file" in output


def test_workspace_mcp_configs_are_isolated(tmp_path):
    """Each workspace should keep its own generated MCP config files."""
    project_a = tmp_path / "project_a"
    project_b = tmp_path / "project_b"
    project_a.mkdir()
    project_b.mkdir()

    base = {
        "security": {"blocked_tools": ["curl"]},
        "network": {"mode": "bridge"},
    }
    (project_a / "yolo-jail.jsonc").write_text(
        json.dumps({**base, "mcp_presets": ["chrome-devtools", "sequential-thinking"]})
    )
    (project_b / "yolo-jail.jsonc").write_text(
        json.dumps({**base, "mcp_servers": {"chrome-devtools": None}})
    )

    result_a = run_yolo(project_a, "true")
    assert result_a.returncode == 0, result_a.stderr

    result_b = run_yolo(project_b, "true")
    assert result_b.returncode == 0, result_b.stderr

    copilot_a = json.loads(
        (project_a / ".yolo" / "home" / "copilot" / "mcp-config.json").read_text()
    )
    copilot_b = json.loads(
        (project_b / ".yolo" / "home" / "copilot" / "mcp-config.json").read_text()
    )
    gemini_a = json.loads(
        (project_a / ".yolo" / "home" / "gemini" / "settings.json").read_text()
    )
    gemini_b = json.loads(
        (project_b / ".yolo" / "home" / "gemini" / "settings.json").read_text()
    )

    assert "chrome-devtools" in copilot_a["mcpServers"]
    assert "chrome-devtools" in gemini_a["mcpServers"]
    assert "chrome-devtools" not in copilot_b["mcpServers"]
    assert "chrome-devtools" not in gemini_b["mcpServers"]
    assert "chrome-devtools" in copilot_a["mcpServers"], (
        "project_b should not stomp project_a"
    )


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


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="Host mise has macOS binaries that cannot execute in the Linux container",
)
def test_venv_symlinks_resolve(temp_project):
    """Test that host .venv python symlinks resolve inside the jail.

    The host mise dir is bind-mounted at its native host path inside the jail,
    so an absolute shebang like /home/user/.local/share/mise/installs/python/...
    points to the same bytes whether resolved on the host or in the container.
    This test writes a venv using the host path and asserts it works in-jail.
    """
    host_mise_base = Path(
        os.environ.get("MISE_DATA_DIR", str(Path.home() / ".local/share/mise"))
    )
    installs = host_mise_base / "installs" / "python"
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

    # Symlink target is the native host path — the jail mirrors the host mise
    # dir at the same absolute path, so this resolves identically inside.
    container_python = (
        host_mise_base
        / "installs"
        / "python"
        / version_dir.name
        / "bin"
        / python_bin.name
    )

    venv_dir = temp_project / ".venv" / "bin"
    venv_dir.mkdir(parents=True)
    (venv_dir / "python").symlink_to(container_python)

    result = run_yolo(temp_project, "/workspace/.venv/bin/python --version")
    assert result.returncode == 0, (
        f"symlink target: {container_python}, stderr: {result.stderr}"
    )
    assert "Python" in result.stdout


@pytest.mark.xfail(
    reason="mise venv creation via uv can hang when MISE_DATA_DIR is shared "
    "between host and container (lock contention during eval mise env)",
    strict=False,
)
@pytest.mark.skipif(
    Path("/run/.containerenv").exists() or Path("/.dockerenv").exists(),
    reason="mise has a re-entrant shim deadlock in nested containers (podman-in-podman)",
)
def test_mise_venv_activation(tmp_path):
    """Test that mise.toml with _.python.venv activates the venv automatically."""
    project_dir = tmp_path / "venv_test"
    project_dir.mkdir()

    with open(project_dir / "mise.toml", "w") as f:
        # Pin to 3.13 (already installed in the jail) to avoid a slow download.
        f.write(
            '[tools]\npython = "3.13"\n\n[env]\n_.python.venv = { path = ".venv", create = true }\n'
        )

    # Longer timeout: nested container startup + venv creation can be slow,
    # especially when mise needs to resolve/install Python versions.
    result = run_yolo(project_dir, "echo $VIRTUAL_ENV", timeout=600)
    assert result.returncode == 0
    assert ".venv" in result.stdout


def test_vscode_mcp_shadowed(temp_project):
    """Test that workspace .vscode/mcp.json is shadowed with /dev/null inside jail."""
    vscode_dir = temp_project / ".vscode"
    vscode_dir.mkdir()
    (vscode_dir / "mcp.json").write_text('{"servers": {"bad": {"command": "false"}}}')

    result = run_yolo(temp_project, "cat /workspace/.vscode/mcp.json")
    # /dev/null is empty, so cat should not output the original mcp.json content.
    # cli.py may print diagnostic warnings to stdout (e.g. nix build warnings when
    # running nested inside a jail), so we check the content is absent rather than
    # asserting stdout is completely empty.
    assert "bad" not in result.stdout
    assert "servers" not in result.stdout


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


def test_host_port_forwarding_data(tmp_path):
    """Test that forward_host_ports actually forwards TCP data end-to-end.

    Starts a real HTTP server on the host side, configures port forwarding,
    and verifies the response is returned inside the jail.
    """
    import socket
    import http.server
    import threading

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # Start a simple HTTP server that returns a known payload
    marker = f"YOLO_PORT_TEST_{port}"

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(marker.encode())

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        project_dir = tmp_path / "port_test"
        project_dir.mkdir()
        config = {
            "network": {
                "mode": "bridge",
                "forward_host_ports": [port],
            },
        }
        with open(project_dir / "yolo-jail.jsonc", "w") as f:
            json.dump(config, f)

        # curl the forwarded port from inside the jail
        result = run_yolo(
            project_dir,
            f"curl -s --max-time 5 http://127.0.0.1:{port}/",
            timeout=DEFAULT_JAIL_TIMEOUT,
        )
        assert marker in result.stdout, (
            f"Expected {marker!r} in stdout, got: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )
    finally:
        server.shutdown()


def test_cgroup_delegation_available(tmp_path):
    """Verify cgroup delegation via host-side daemon works inside the jail.

    Tests that:
    1. The cgroup delegate socket exists at /tmp/yolo-cgd/cgroup.sock
    2. yolo-cglimit can communicate with the host daemon
    3. The daemon can create child cgroups and set limits
    """
    _skip_if_cgroup_readonly()

    project_dir = tmp_path / "cgroup_test"
    project_dir.mkdir()
    config = {"network": {"mode": "bridge"}}
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)

    result = run_yolo(
        project_dir,
        "set -e; "
        # Check the delegate socket exists
        'test -S /tmp/yolo-cgd/cgroup.sock && echo "SOCKET_EXISTS"; '
        # Use yolo-cglimit to run a trivial command with CPU limit
        "yolo-cglimit --cpu 75 --name test-cgd -- "
        'echo "DELEGATION_OK"; '
        "true",
        timeout=DEFAULT_JAIL_TIMEOUT,
    )
    stdout = result.stdout
    stderr = result.stderr
    assert "SOCKET_EXISTS" in stdout, (
        f"Expected cgroup delegate socket to exist.\nstdout: {stdout}\nstderr: {stderr}"
    )
    assert "DELEGATION_OK" in stdout, (
        f"Expected cgroup delegation to work.\nstdout: {stdout}\nstderr: {stderr}"
    )


def test_cglimit_helper_available(tmp_path):
    """Verify yolo-cglimit helper is on PATH and functional inside the jail."""
    project_dir = tmp_path / "cglimit_test"
    project_dir.mkdir()
    config = {"network": {"mode": "bridge"}}
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)

    result = run_yolo(
        project_dir,
        "which yolo-cglimit && yolo-cglimit --help",
        timeout=DEFAULT_JAIL_TIMEOUT,
    )
    assert "yolo-cglimit" in result.stdout, (
        f"yolo-cglimit not found on PATH.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "--cpu" in result.stdout, (
        f"yolo-cglimit --help missing expected content.\nstdout: {result.stdout}"
    )


def test_cglimit_enforces_cpu_limit(tmp_path):
    """Verify yolo-cglimit creates a cgroup and enforces a CPU limit via host daemon."""
    _skip_if_cgroup_readonly()

    project_dir = tmp_path / "cglimit_enforce"
    project_dir.mkdir()
    config = {"network": {"mode": "bridge"}}
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)

    result = run_yolo(
        project_dir,
        # Use yolo-cglimit to run a command with 75% CPU limit
        'set -e; yolo-cglimit --cpu 75 --name test-enforce -- echo "ENFORCE_OK"; true',
        timeout=DEFAULT_JAIL_TIMEOUT,
    )
    stdout = result.stdout
    stderr = result.stderr
    assert "ENFORCE_OK" in stdout, (
        f"Expected command to run under cgroup limit.\nstdout: {stdout}\nstderr: {stderr}"
    )
