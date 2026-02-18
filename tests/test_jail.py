import os
import subprocess
import json
import shutil
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
YOLO_CMD = REPO_ROOT / "yolo-enter.sh"

@pytest.fixture
def temp_project(tmp_path):
    """Create a temporary project directory with a yolo-jail.jsonc."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()
    
    config = {
        "security": {
            "blocked_tools": [
                "curl",
                {"name": "grep", "message": "NO GREP ALLOWED", "suggestion": "use rg"}
            ]
        },
        "network": {"mode": "bridge"}
    }
    
    with open(project_dir / "yolo-jail.jsonc", "w") as f:
        json.dump(config, f)
        
    return project_dir

def run_yolo(project_dir, command):
    """Run a shell command inside the jail."""
    result = subprocess.run(
        [str(YOLO_CMD), "--", "bash", "-lc", command],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        env={**os.environ, "TERM": "dumb"} # Avoid color codes in output if possible
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
    
    subprocess.run([str(YOLO_CMD), "init"], cwd=str(project_dir), check=True)
    
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

@pytest.mark.skip(reason="Agents (gemini/copilot) must be installed via mise in target project. Bootstrap script is optional; agents only install if user manually runs bootstrap or includes them in project mise.toml.")
def test_agent_tools_available(temp_project):
    """Test that gemini and copilot are available."""
    # We can't rely on them being installed in the temp_project context unless we force it
    # But yolo-jail should provide them if they are in the MAIN project's mise.toml
    # Wait, the jail uses the mise.toml from the TARGET directory.
    
    # So we need to add them to the temp_project's mise.toml
    with open(temp_project / "mise.toml", "w") as f:
        f.write('[tools]\nnode = "system"\npython = "system"\n"npm:@google/gemini-cli" = "latest"\n"npm:@github/copilot" = "latest"\n')
        
    # We expect mise to install them (mocked/cached ideally, but here real)
    # This might be slow for a test, but it verifies the end-to-end flow.
    result = run_yolo(temp_project, "gemini --version && copilot --version")
    assert result.returncode == 0

def test_yolo_direct_command(tmp_path):
    """Test running a direct command like 'yolo -- ls'."""
    project_dir = tmp_path / "direct_cmd_test"
    project_dir.mkdir()
    
    # Run yolo with the explicit -- delimiter
    result = subprocess.run(
        [str(YOLO_CMD), "--", "ls", "-d", "/workspace"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        env={**os.environ, "TERM": "dumb"}
    )
    assert result.returncode == 0
    assert "/workspace" in result.stdout

def test_jail_configs_present(temp_project):
    """Test that the persistent jail configs (YOLO mode) are visible."""
    # We check if the files we just created in the global storage are visible inside
    result = run_yolo(temp_project, "ls /home/agent/.copilot/config.json && ls /home/agent/.gemini/settings.json")
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
        mise_base = Path(os.environ.get("MISE_DATA_DIR", str(Path.home() / ".local/share/mise")))

    installs = mise_base / "installs" / "python"
    if not installs.exists():
        pytest.skip("No mise python installs found")

    versions = [d for d in installs.iterdir() if d.is_dir() and not d.is_symlink() and (d / "bin").exists()]
    if not versions:
        pytest.skip("No mise python installs found")

    # Pick the first concrete version dir (not a symlink like "3" → "3.13.12")
    version_dir = versions[0]
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
    assert result.returncode == 0
    assert "Python" in result.stdout


def test_mise_venv_activation(tmp_path):
    """Test that mise.toml with _.python.venv activates the venv automatically."""
    project_dir = tmp_path / "venv_test"
    project_dir.mkdir()

    with open(project_dir / "mise.toml", "w") as f:
        f.write('[tools]\npython = "3"\n\n[env]\n_.python.venv = { path = ".venv", create = true }\n')

    # python should be the venv python, with VIRTUAL_ENV set
    result = run_yolo(project_dir, "echo $VIRTUAL_ENV")
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
