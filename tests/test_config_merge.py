import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import cli
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


def test_same_file_preset_null_conflict_is_reported():
    conflicts = cli._check_preset_null_conflicts(
        {
            "mcp_presets": ["chrome-devtools", "sequential-thinking"],
            "mcp_servers": {"chrome-devtools": None},
        },
        "yolo-jail.jsonc",
    )

    assert conflicts == [
        "yolo-jail.jsonc: preset 'chrome-devtools' is enabled in mcp_presets but "
        "null-removed in mcp_servers within the same config file"
    ]


def test_cross_hierarchy_preset_null_override_is_allowed():
    user = {"mcp_presets": ["chrome-devtools", "sequential-thinking"]}
    workspace = {"mcp_servers": {"chrome-devtools": None}}

    merged = merge_config(user, workspace)

    assert cli._check_preset_null_conflicts(user, "user") == []
    assert cli._check_preset_null_conflicts(workspace, "workspace") == []
    assert cli._effective_mcp_server_names(
        merged.get("mcp_servers"), merged.get("mcp_presets")
    ) == ["sequential-thinking"]


def test_init_per_workspace_mcp_configs_seeds_gemini_settings(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared-home"
    (shared_home / ".gemini").mkdir(parents=True)
    (shared_home / ".gemini" / "settings.json").write_text(
        json.dumps(
            {
                "security": {"approvalMode": "yolo"},
                "general": {"previewFeatures": True},
                "mcpServers": {"chrome-devtools": {"command": "/bin/node"}},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(cli, "GLOBAL_HOME", shared_home)

    ws_state = tmp_path / "workspace" / ".yolo" / "home"
    ws_state.mkdir(parents=True)

    cli._init_per_workspace_mcp_configs(ws_state)

    assert json.loads((ws_state / "copilot-mcp-config.json").read_text()) == {}
    assert json.loads((ws_state / "copilot-lsp-config.json").read_text()) == {}
    assert json.loads((ws_state / "gemini-managed-mcp.json").read_text()) == []
    seeded = json.loads((ws_state / "gemini-settings.json").read_text())
    assert seeded["security"]["approvalMode"] == "yolo"
    assert seeded["general"]["previewFeatures"] is True
    assert "mcpServers" not in seeded


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
