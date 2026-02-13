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
    """Create a temporary project directory with a yolo-jail.json."""
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
    
    with open(project_dir / "yolo-jail.json", "w") as f:
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
    """Test that grep is blocked with custom message."""
    result = run_yolo(temp_project, "grep 'foo' bar")
    assert result.returncode == 127
    assert "NO GREP ALLOWED" in result.stderr
    assert "Suggestion: use rg" in result.stderr

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
    
    assert (project_dir / "yolo-jail.json").exists()
