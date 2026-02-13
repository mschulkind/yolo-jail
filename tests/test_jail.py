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
    """Run a command inside the jail."""
    # We use subprocess.run directly on the shell script
    result = subprocess.run(
        [str(YOLO_CMD), "run", command],
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
    """Test that grep is blocked interactively but works in scripts (smart shim)."""
    # Since pytest runs non-interactively, the shim should PASS THROUGH to real grep
    # Real grep returns 2 if file not found
    result = run_yolo(temp_project, "grep 'foo' bar")
    assert result.returncode == 2
    assert "No such file" in result.stderr

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
    """Test running a direct command like 'yolo ls'."""
    project_dir = tmp_path / "direct_cmd_test"
    project_dir.mkdir()
    
    # Run yolo without explicitly saying 'run', just the command
    result = subprocess.run(
        [str(YOLO_CMD), "ls", "-d", "/workspace"],
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
    result = run_yolo(temp_project, "ls /home/agent/.config/.copilot/config.json && ls /home/agent/.gemini/settings.json")
    assert result.returncode == 0
