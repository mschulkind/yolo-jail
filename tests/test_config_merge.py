import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cli import (
    ConfigError,
    _check_config_changes,
    _load_jsonc_file,
    _validate_config,
    merge_config,
)


def test_merge_config_lists_dedup_and_override_scalars():
    user = {
        "packages": ["sqlite", "postgresql"],
        "mounts": ["~/code/shared:/ctx/shared"],
        "network": {"mode": "bridge"},
        "security": {"blocked_tools": ["wget", {"name": "grep"}]},
    }
    workspace = {
        "packages": ["postgresql", "redis"],
        "mounts": ["~/code/extra:/ctx/extra"],
        "network": {"mode": "host"},
        "security": {"blocked_tools": [{"name": "grep"}, "curl"]},
    }

    merged = merge_config(user, workspace)

    assert merged["packages"] == ["sqlite", "postgresql", "redis"]
    assert merged["mounts"] == ["~/code/shared:/ctx/shared", "~/code/extra:/ctx/extra"]
    assert merged["network"]["mode"] == "host"
    assert merged["security"]["blocked_tools"] == ["wget", {"name": "grep"}, "curl"]


def test_merge_config_merges_mcp_servers_dicts():
    user = {
        "mcp_servers": {
            "foo": {"command": "/bin/foo", "args": ["--a"]},
            "bar": {"command": "/bin/bar", "args": []},
        }
    }
    workspace = {
        "mcp_servers": {
            "bar": {"command": "/workspace/bar", "args": ["--override"]},
            "baz": {"command": "/workspace/baz", "args": []},
        }
    }

    merged = merge_config(user, workspace)

    assert merged["mcp_servers"]["foo"]["command"] == "/bin/foo"
    assert merged["mcp_servers"]["bar"]["command"] == "/workspace/bar"
    assert merged["mcp_servers"]["baz"]["command"] == "/workspace/baz"


def test_merge_config_mcp_servers_can_disable_inherited_server():
    user = {
        "mcp_servers": {
            "foo": {"command": "/bin/foo", "args": ["--a"]},
        }
    }
    workspace = {
        "mcp_servers": {
            "foo": None,
        }
    }

    merged = merge_config(user, workspace)

    assert merged["mcp_servers"]["foo"] is None


def test_load_jsonc_file_strict_raises_for_invalid_json(tmp_path):
    config_path = tmp_path / "yolo-jail.jsonc"
    config_path.write_text("{invalid json")

    with pytest.raises(ConfigError):
        _load_jsonc_file(config_path, "yolo-jail.jsonc", strict=True)


def test_validate_config_rejects_unknown_top_level_keys():
    errors, warnings = _validate_config({"mcp_server": {}}, workspace=Path.cwd())

    assert warnings == []
    assert "config.mcp_server: unknown key" in errors


def test_validate_config_requires_file_extensions_for_lsp_servers():
    errors, warnings = _validate_config(
        {
            "lsp_servers": {
                "python": {
                    "command": "/custom/pyright",
                    "args": ["--stdio"],
                }
            }
        },
        workspace=Path.cwd(),
    )

    assert warnings == []
    assert "config.lsp_servers.python.fileExtensions: expected an object" in errors


class TestConfigSnapshot:
    def test_first_run_saves_snapshot(self, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        config = {"packages": ["strace"]}
        assert _check_config_changes(workspace, config) is True
        snapshot = workspace / ".yolo" / "config-snapshot.json"
        assert snapshot.exists()
        assert json.loads(snapshot.read_text()) == config

    def test_unchanged_config_passes(self, tmp_path):
        workspace = tmp_path / "project"
        workspace.mkdir()
        config = {"packages": ["strace"]}
        _check_config_changes(workspace, config)
        assert _check_config_changes(workspace, config) is True

    def test_changed_config_rejects_on_no(self, tmp_path, monkeypatch):
        workspace = tmp_path / "project"
        workspace.mkdir()
        config = {"packages": ["strace"]}
        _check_config_changes(workspace, config)

        # Simulate non-interactive (no tty) — should auto-accept
        monkeypatch.setattr("sys.stdin", open("/dev/null"))
        new_config = {"packages": ["strace", "htop"]}
        assert _check_config_changes(workspace, new_config) is True

    def test_changed_config_interactive_yes(self, tmp_path, monkeypatch):
        workspace = tmp_path / "project"
        workspace.mkdir()
        config = {"packages": ["strace"]}
        _check_config_changes(workspace, config)

        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
        # Make isatty return True on our StringIO
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        new_config = {"packages": ["strace", "htop"]}
        assert _check_config_changes(workspace, new_config) is True
        # Snapshot should be updated
        snapshot = json.loads(
            (workspace / ".yolo" / "config-snapshot.json").read_text()
        )
        assert snapshot == new_config

    def test_changed_config_interactive_no(self, tmp_path, monkeypatch):
        workspace = tmp_path / "project"
        workspace.mkdir()
        config = {"packages": ["strace"]}
        _check_config_changes(workspace, config)

        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        new_config = {"packages": ["strace", "htop"]}
        assert _check_config_changes(workspace, new_config) is False
        # Snapshot should NOT be updated
        snapshot = json.loads(
            (workspace / ".yolo" / "config-snapshot.json").read_text()
        )
        assert snapshot == config
