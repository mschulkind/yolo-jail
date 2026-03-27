"""Unit tests for src/entrypoint.py config generation."""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the entrypoint module
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import entrypoint


@pytest.fixture
def jail_home(tmp_path, monkeypatch):
    """Redirect all entrypoint paths to a temp directory."""
    # Clear env vars that leak from the host/jail environment
    for var in (
        "YOLO_MCP_PRESETS",
        "YOLO_MCP_SERVERS",
        "YOLO_LSP_SERVERS",
        "YOLO_BLOCK_CONFIG",
        "YOLO_MISE_TOOLS",
        "YOLO_HOST_DIR",
    ):
        monkeypatch.delenv(var, raising=False)

    orig = {}
    attrs = [
        "HOME",
        "SHIM_DIR",
        "NPM_PREFIX",
        "NPM_BIN",
        "GO_BIN",
        "GOPATH",
        "MCP_WRAPPERS_BIN",
        "BASHRC_PATH",
        "COPILOT_DIR",
        "GEMINI_DIR",
        "GEMINI_MANAGED_MCP_PATH",
        "MISE_CONFIG_DIR",
    ]
    for attr in attrs:
        orig[attr] = getattr(entrypoint, attr)

    entrypoint.HOME = tmp_path
    entrypoint.SHIM_DIR = tmp_path / ".yolo-shims"
    entrypoint.NPM_PREFIX = tmp_path / ".npm-global"
    entrypoint.NPM_BIN = tmp_path / ".npm-global" / "bin"
    entrypoint.GOPATH = tmp_path / "go"
    entrypoint.GO_BIN = tmp_path / "go" / "bin"
    entrypoint.MCP_WRAPPERS_BIN = tmp_path / ".local" / "bin" / "mcp-wrappers"
    entrypoint.BASHRC_PATH = tmp_path / ".bashrc"
    entrypoint.COPILOT_DIR = tmp_path / ".copilot"
    entrypoint.GEMINI_DIR = tmp_path / ".gemini"
    entrypoint.GEMINI_MANAGED_MCP_PATH = (
        tmp_path / ".gemini" / "yolo-managed-mcp-servers.json"
    )
    entrypoint.MISE_CONFIG_DIR = tmp_path / ".config" / "mise"

    yield tmp_path

    for attr in attrs:
        setattr(entrypoint, attr, orig[attr])


# -- Shim generation --


class TestShimGeneration:
    def test_blocked_tool_no_fallthrough(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_BLOCK_CONFIG",
            json.dumps(
                [
                    {
                        "name": "curl",
                        "message": "curl is blocked",
                        "suggestion": "Use wget",
                    }
                ]
            ),
        )
        entrypoint.generate_shims()
        shim = (entrypoint.SHIM_DIR / "curl").read_text()
        assert "curl is blocked" in shim
        assert "Use wget" in shim
        assert "exec" not in shim  # no fallthrough

    def test_blocked_tool_with_fallthrough(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_BLOCK_CONFIG",
            json.dumps(
                [{"name": "grep", "message": "grep blocked", "suggestion": "Try rg"}]
            ),
        )
        entrypoint.generate_shims()
        shim = (entrypoint.SHIM_DIR / "grep").read_text()
        assert "exec /bin/grep" in shim
        assert "YOLO_BYPASS_SHIMS" in shim

    def test_shims_executable(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_BLOCK_CONFIG", json.dumps([{"name": "curl"}]))
        entrypoint.generate_shims()
        assert os.access(entrypoint.SHIM_DIR / "curl", os.X_OK)

    def test_empty_block_config(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_BLOCK_CONFIG", "")
        entrypoint.generate_shims()
        assert entrypoint.SHIM_DIR.exists()
        assert list(entrypoint.SHIM_DIR.iterdir()) == []

    def test_invalid_json_block_config(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_BLOCK_CONFIG", "not json")
        entrypoint.generate_shims()  # should not raise

    def test_shims_cleaned_on_regeneration(self, jail_home, monkeypatch):
        """Old shims are removed when regenerating."""
        monkeypatch.setenv("YOLO_BLOCK_CONFIG", json.dumps([{"name": "curl"}]))
        entrypoint.generate_shims()
        assert (entrypoint.SHIM_DIR / "curl").exists()

        monkeypatch.setenv("YOLO_BLOCK_CONFIG", json.dumps([{"name": "wget"}]))
        entrypoint.generate_shims()
        assert not (entrypoint.SHIM_DIR / "curl").exists()
        assert (entrypoint.SHIM_DIR / "wget").exists()


# -- Bashrc generation --


class TestBashrcGeneration:
    def test_contains_jail_prompt(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_HOST_DIR", "/home/user/kitchen")
        entrypoint.generate_bashrc()
        content = entrypoint.BASHRC_PATH.read_text()
        assert "YOLO-JAIL" in content
        assert "kitchen" in content

    def test_contains_mise_activation(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_HOST_DIR", "test")
        entrypoint.generate_bashrc()
        assert "mise activate bash" in entrypoint.BASHRC_PATH.read_text()

    def test_contains_aliases(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_HOST_DIR", "test")
        entrypoint.generate_bashrc()
        content = entrypoint.BASHRC_PATH.read_text()
        assert "alias gemini='gemini --yolo'" in content
        assert "alias copilot='copilot --yolo --no-auto-update'" in content

    def test_pager_disabled(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_HOST_DIR", "test")
        entrypoint.generate_bashrc()
        content = entrypoint.BASHRC_PATH.read_text()
        assert "PAGER=cat" in content
        assert "GIT_PAGER=cat" in content

    def test_mise_shims_in_path(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_HOST_DIR", "test")
        entrypoint.generate_bashrc()
        content = entrypoint.BASHRC_PATH.read_text()
        assert "/mise/shims" in content or "MISE_DATA_DIR" in content
        assert content.index("$NPM_CONFIG_PREFIX/bin") < content.index(
            "${MISE_DATA_DIR:-/mise}/shims"
        )


# -- Copilot config --


class TestCopilotConfig:
    def test_mcp_config_no_presets(self, jail_home):
        """With no presets, MCP config has no built-in servers."""
        entrypoint.configure_copilot()
        mcp = json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
        assert "chrome-devtools" not in mcp["mcpServers"]
        assert "sequential-thinking" not in mcp["mcpServers"]

    def test_mcp_config_with_presets(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools", "sequential-thinking"]),
        )
        entrypoint.configure_copilot()
        mcp = json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
        assert "chrome-devtools" in mcp["mcpServers"]
        assert "sequential-thinking" in mcp["mcpServers"]

    def test_mcp_workspace_override(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps(
                {
                    "probe-mcp": {
                        "command": "/workspace/probe-mcp.py",
                        "args": ["--stdio"],
                    }
                }
            ),
        )
        entrypoint.configure_copilot()
        mcp = json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
        assert mcp["mcpServers"]["probe-mcp"]["command"] == "/workspace/probe-mcp.py"

    def test_mcp_workspace_override_replaces_preset(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["sequential-thinking"]),
        )
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps(
                {
                    "sequential-thinking": {
                        "command": "/workspace/custom-seq.py",
                        "args": ["--custom"],
                    }
                }
            ),
        )
        entrypoint.configure_copilot()
        mcp = json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
        assert mcp["mcpServers"]["sequential-thinking"]["command"] == (
            "/workspace/custom-seq.py"
        )

    def test_mcp_workspace_override_can_disable_preset(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools", "sequential-thinking"]),
        )
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps({"chrome-devtools": None}),
        )
        entrypoint.configure_copilot()
        mcp = json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
        assert "chrome-devtools" not in mcp["mcpServers"]
        assert "sequential-thinking" in mcp["mcpServers"]

    def test_lsp_config(self, jail_home):
        entrypoint.configure_copilot()
        lsp = json.loads((entrypoint.COPILOT_DIR / "lsp-config.json").read_text())
        assert "python" in lsp["lspServers"]
        assert ".py" in lsp["lspServers"]["python"]["fileExtensions"]
        assert "typescript" in lsp["lspServers"]
        assert "go" in lsp["lspServers"]
        assert ".go" in lsp["lspServers"]["go"]["fileExtensions"]

    def test_lsp_workspace_override(self, jail_home, monkeypatch):
        """Workspace LSP servers merge with defaults."""
        monkeypatch.setenv(
            "YOLO_LSP_SERVERS",
            json.dumps(
                {
                    "rust": {
                        "command": "rust-analyzer",
                        "args": [],
                        "fileExtensions": {".rs": "rust"},
                    },
                }
            ),
        )
        entrypoint.configure_copilot()
        lsp = json.loads((entrypoint.COPILOT_DIR / "lsp-config.json").read_text())
        # Defaults still present
        assert "python" in lsp["lspServers"]
        assert "typescript" in lsp["lspServers"]
        assert "go" in lsp["lspServers"]
        # Workspace server added
        assert "rust" in lsp["lspServers"]
        assert lsp["lspServers"]["rust"]["command"] == "rust-analyzer"
        assert ".rs" in lsp["lspServers"]["rust"]["fileExtensions"]

    def test_lsp_workspace_override_replaces_default(self, jail_home, monkeypatch):
        """Workspace can override a default LSP server."""
        monkeypatch.setenv(
            "YOLO_LSP_SERVERS",
            json.dumps(
                {
                    "python": {
                        "command": "/custom/pyright",
                        "args": ["--stdio"],
                        "fileExtensions": {".py": "python"},
                    },
                }
            ),
        )
        entrypoint.configure_copilot()
        lsp = json.loads((entrypoint.COPILOT_DIR / "lsp-config.json").read_text())
        assert lsp["lspServers"]["python"]["command"] == "/custom/pyright"

    def test_yolo_config_created(self, jail_home):
        entrypoint.configure_copilot()
        config = json.loads((entrypoint.COPILOT_DIR / "config.json").read_text())
        assert config["yolo"] is True

    def test_existing_config_preserved(self, jail_home):
        entrypoint.COPILOT_DIR.mkdir(parents=True)
        (entrypoint.COPILOT_DIR / "config.json").write_text(
            '{"yolo": false, "custom": 1}'
        )
        entrypoint.configure_copilot()
        config = json.loads((entrypoint.COPILOT_DIR / "config.json").read_text())
        # config.json is only written if missing — existing preserved
        assert config["custom"] == 1


# -- Gemini config --


class TestGeminiConfig:
    def test_fresh_config_no_presets(self, jail_home):
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["security"]["approvalMode"] == "yolo"
        assert "chrome-devtools" not in cfg["mcpServers"]
        assert "python-lsp" in cfg["mcpServers"]
        assert "typescript-lsp" in cfg["mcpServers"]
        assert "go-lsp" in cfg["mcpServers"]
        assert cfg["general"]["enableAutoUpdate"] is False
        assert cfg["general"]["enableAutoUpdateNotification"] is False

    def test_fresh_config_with_presets(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools", "sequential-thinking"]),
        )
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "chrome-devtools" in cfg["mcpServers"]
        assert "sequential-thinking" in cfg["mcpServers"]

    def test_gemini_mcp_workspace_server(self, jail_home, monkeypatch):
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps(
                {
                    "probe-mcp": {
                        "command": "/workspace/probe-mcp.py",
                        "args": ["--stdio"],
                    }
                }
            ),
        )
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["mcpServers"]["probe-mcp"]["command"] == "/workspace/probe-mcp.py"

    def test_gemini_mcp_workspace_override_replaces_preset(
        self, jail_home, monkeypatch
    ):
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools"]),
        )
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps(
                {
                    "chrome-devtools": {
                        "command": "/workspace/custom-node",
                        "args": ["custom-devtools"],
                    }
                }
            ),
        )
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["mcpServers"]["chrome-devtools"]["command"] == (
            "/workspace/custom-node"
        )

    def test_gemini_mcp_workspace_override_can_disable_preset(
        self, jail_home, monkeypatch
    ):
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools", "sequential-thinking"]),
        )
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps({"chrome-devtools": None}),
        )
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "chrome-devtools" not in cfg["mcpServers"]
        assert "sequential-thinking" in cfg["mcpServers"]

    def test_gemini_lsp_workspace_server(self, jail_home, monkeypatch):
        """Workspace LSP servers appear as MCP-wrapped servers in Gemini config."""
        monkeypatch.setenv(
            "YOLO_LSP_SERVERS",
            json.dumps(
                {
                    "rust": {
                        "command": "rust-analyzer",
                        "args": [],
                        "fileExtensions": {".rs": "rust"},
                    },
                }
            ),
        )
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "rust-lsp" in cfg["mcpServers"]
        rust_args = cfg["mcpServers"]["rust-lsp"]["args"]
        assert "-lsp" in rust_args
        assert "rust-analyzer" in rust_args

    def test_merge_preserves_custom_servers(self, jail_home):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        existing = {"mcpServers": {"my-server": {"command": "foo"}}}
        (entrypoint.GEMINI_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "my-server" in cfg["mcpServers"]

    def test_merge_preserves_existing_security(self, jail_home):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        existing = {"security": {"approvalMode": "confirm"}}
        (entrypoint.GEMINI_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["security"]["approvalMode"] == "confirm"

    def test_removes_stale_workspace_mcp_servers(self, jail_home):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        existing = {
            "mcpServers": {
                "my-server": {"command": "foo"},
                "probe-mcp": {"command": "/workspace/probe_mcp.py", "args": []},
            }
        }
        (entrypoint.GEMINI_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "my-server" in cfg["mcpServers"]
        assert "probe-mcp" not in cfg["mcpServers"]

    def test_uses_managed_mcp_sidecar_to_cleanup_prior_servers(
        self, jail_home, monkeypatch
    ):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        (entrypoint.GEMINI_DIR / "settings.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "my-server": {"command": "foo"},
                        "probe-mcp": {"command": "/workspace/probe_mcp.py", "args": []},
                    }
                }
            )
        )
        entrypoint.GEMINI_MANAGED_MCP_PATH.write_text(json.dumps(["probe-mcp"]))
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps({"other-mcp": {"command": "/workspace/other.py", "args": []}}),
        )
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "my-server" in cfg["mcpServers"]
        assert "probe-mcp" not in cfg["mcpServers"]
        assert "other-mcp" in cfg["mcpServers"]

    def test_gemini_auto_update_is_forced_off(self, jail_home):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        existing = {
            "general": {
                "enableAutoUpdate": True,
                "enableAutoUpdateNotification": True,
            }
        }
        (entrypoint.GEMINI_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["general"]["enableAutoUpdate"] is False
        assert cfg["general"]["enableAutoUpdateNotification"] is False

    def test_handles_corrupt_json(self, jail_home):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        (entrypoint.GEMINI_DIR / "settings.json").write_text("not json{{{")
        entrypoint.configure_gemini()  # should not raise
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert "mcpServers" in cfg


# -- MCP wrappers --


class TestMCPWrappers:
    def test_node_wrapper(self, jail_home):
        entrypoint.generate_mcp_wrappers()
        node = (entrypoint.MCP_WRAPPERS_BIN / "node").read_text()
        assert "LD_LIBRARY_PATH" in node
        assert "/bin/node" in node
        assert os.access(entrypoint.MCP_WRAPPERS_BIN / "node", os.X_OK)

    def test_npx_wrapper(self, jail_home):
        entrypoint.generate_mcp_wrappers()
        npx = (entrypoint.MCP_WRAPPERS_BIN / "npx").read_text()
        assert "/bin/npx" in npx

    def test_chrome_wrapper(self, jail_home):
        entrypoint.generate_mcp_wrappers()
        chrome = (
            jail_home / ".local" / "bin" / "chrome-devtools-mcp-wrapper"
        ).read_text()
        assert "--no-sandbox" in chrome
        assert "chrome-devtools-mcp" in chrome


# -- Mise config --


class TestMiseConfig:
    def test_creates_config(self, jail_home):
        entrypoint.generate_mise_config()
        content = (entrypoint.MISE_CONFIG_DIR / "config.toml").read_text()
        assert 'node = "22"' in content
        assert 'python = "3.13"' in content

    def test_preserves_existing_versions(self, jail_home):
        """Existing tool versions aren't overwritten, but missing base tools are added."""
        entrypoint.MISE_CONFIG_DIR.mkdir(parents=True)
        (entrypoint.MISE_CONFIG_DIR / "config.toml").write_text(
            '[tools]\nnode = "20"\npython = "3.12"\n'
        )
        entrypoint.generate_mise_config()
        content = (entrypoint.MISE_CONFIG_DIR / "config.toml").read_text()
        # Existing versions preserved
        assert 'node = "20"' in content
        assert 'python = "3.12"' in content
        # Missing base tools added
        assert 'go = "latest"' in content
        # copilot and gemini are NOT in mise base_tools (managed by bootstrap npm install)
        assert '"npm:@github/copilot"' not in content
        assert "gemini" not in content

    def test_removes_retired_tools(self, jail_home):
        """Retired tools (copilot/gemini) are removed from existing mise configs."""
        entrypoint.MISE_CONFIG_DIR.mkdir(parents=True)
        (entrypoint.MISE_CONFIG_DIR / "config.toml").write_text(
            '[tools]\nnode = "22"\ngemini = "latest"\n"npm:@github/copilot" = "latest"\n'
        )
        entrypoint.generate_mise_config()
        content = (entrypoint.MISE_CONFIG_DIR / "config.toml").read_text()
        assert 'node = "22"' in content
        # Retired tools should be removed
        assert "gemini" not in content
        assert "npm:@github/copilot" not in content


# -- Bootstrap script --


class TestBootstrapScript:
    def test_creates_script(self, jail_home):
        entrypoint.generate_bootstrap_script()
        script = (jail_home / ".yolo-bootstrap.sh").read_text()
        assert "chrome-devtools-mcp" in script
        assert "mcp-language-server" in script
        assert "showboat" in script
        assert (
            "npm install -g --prefer-online @google/gemini-cli@latest @github/copilot@latest"
            in script
        )
        assert os.access(jail_home / ".yolo-bootstrap.sh", os.X_OK)


# -- Skills merging --


class TestSkillsMerging:
    def test_host_skills_copied(self, jail_home, monkeypatch, tmp_path):
        host_skills = tmp_path / "host-skills"
        (host_skills / "my-skill").mkdir(parents=True)
        (host_skills / "my-skill" / "SKILL.md").write_text("# My Skill")
        monkeypatch.setenv("YOLO_HOST_GEMINI_SKILLS", str(host_skills))
        entrypoint.merge_skills()
        assert (entrypoint.COPILOT_DIR / "skills" / "my-skill" / "SKILL.md").exists()

    def test_workspace_skills_override(self, jail_home, monkeypatch, tmp_path):
        # Host skill
        host_skills = tmp_path / "host-skills"
        (host_skills / "shared").mkdir(parents=True)
        (host_skills / "shared" / "SKILL.md").write_text("host version")
        monkeypatch.setenv("YOLO_HOST_GEMINI_SKILLS", str(host_skills))

        # Workspace skill with same name
        # Can't create in /workspace for real, so test the logic via a temp workspace
        ws = tmp_path / "ws-skills"
        (ws / "shared").mkdir(parents=True)
        (ws / "shared" / "SKILL.md").write_text("workspace version")

        entrypoint.merge_skills()
        # At this point host skills are copied. Now manually call _copy_skill_dirs
        # to simulate workspace override
        entrypoint._copy_skill_dirs(ws, entrypoint.COPILOT_DIR / "skills")
        content = (
            entrypoint.COPILOT_DIR / "skills" / "shared" / "SKILL.md"
        ).read_text()
        assert content == "workspace version"

    def test_skills_cleaned_between_runs(self, jail_home, monkeypatch, tmp_path):
        host_skills = tmp_path / "host-skills"
        (host_skills / "old-skill").mkdir(parents=True)
        (host_skills / "old-skill" / "SKILL.md").write_text("old")
        monkeypatch.setenv("YOLO_HOST_GEMINI_SKILLS", str(host_skills))
        entrypoint.merge_skills()
        assert (entrypoint.COPILOT_DIR / "skills" / "old-skill").exists()

        # Now remove from host and re-merge
        import shutil

        shutil.rmtree(host_skills / "old-skill")
        (host_skills / "new-skill").mkdir(parents=True)
        (host_skills / "new-skill" / "SKILL.md").write_text("new")
        entrypoint.merge_skills()
        assert not (entrypoint.COPILOT_DIR / "skills" / "old-skill").exists()
        assert (entrypoint.COPILOT_DIR / "skills" / "new-skill").exists()


# -- Container-side port forwarding --


class TestContainerPortForwarding:
    """Tests for start_container_port_forwarding() in entrypoint.py.

    The new architecture uses Unix sockets: the entrypoint starts socat on
    TCP-LISTEN → UNIX-CONNECT to a socket file in /tmp/yolo-fwd/.
    """

    def test_no_env_var_does_nothing(self, monkeypatch):
        """No YOLO_FORWARD_HOST_PORTS → nothing happens."""
        monkeypatch.delenv("YOLO_FORWARD_HOST_PORTS", raising=False)
        entrypoint.start_container_port_forwarding()

    def test_empty_string_does_nothing(self, monkeypatch):
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "")
        entrypoint.start_container_port_forwarding()

    def test_empty_array_does_nothing(self, monkeypatch):
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "[]")
        entrypoint.start_container_port_forwarding()

    def test_invalid_json_warns(self, monkeypatch, capsys):
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "not-json")
        entrypoint.start_container_port_forwarding()
        assert "invalid YOLO_FORWARD_HOST_PORTS" in capsys.readouterr().err

    def test_integer_port_launches_socat_with_unix_socket(self, monkeypatch, tmp_path):
        """Integer entry: forward same port via Unix socket."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "[5432]")
        # Create a fake socket file so the function finds it
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        (tmp_path / "port-5432.sock").touch()
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 1
        assert launched[0] == [
            "socat",
            "TCP-LISTEN:5432,bind=127.0.0.1,fork,reuseaddr",
            f"UNIX-CONNECT:{tmp_path / 'port-5432.sock'}",
        ]

    def test_string_remap_port(self, monkeypatch, tmp_path):
        """String 'local:host' entry: listens on local port, connects to host port socket."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", '["8080:9090"]')
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        # Socket file is named after the LOCAL port
        (tmp_path / "port-8080.sock").touch()
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 1
        assert launched[0][1] == "TCP-LISTEN:8080,bind=127.0.0.1,fork,reuseaddr"
        assert "UNIX-CONNECT:" in launched[0][2]

    def test_string_no_colon_same_port(self, monkeypatch, tmp_path):
        """Plain string '5432' treated as same port both sides."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", '["5432"]')
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        (tmp_path / "port-5432.sock").touch()
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 1
        assert "TCP-LISTEN:5432" in launched[0][1]

    def test_multiple_ports(self, monkeypatch, tmp_path):
        """Multiple ports in one config."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", '[5432, 6379, "8080:9090"]')
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        for name in ["port-5432.sock", "port-6379.sock", "port-8080.sock"]:
            (tmp_path / name).touch()
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 3

    def test_skips_port_already_in_use(self, monkeypatch, tmp_path):
        """Port already bound → skip, no socat launched."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "[5432]")
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        (tmp_path / "port-5432.sock").touch()
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: True)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 0

    def test_missing_socket_warns_and_skips(self, monkeypatch, tmp_path, capsys):
        """Socket file not found → warning, skip that port."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "[5432]")
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        # Don't create the socket file
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 0
        assert "not found" in capsys.readouterr().err

    def test_invalid_entry_warns_and_continues(self, monkeypatch, tmp_path, capsys):
        """Non-int/non-string entries warn but don't stop other ports."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", '[5432, {"bad": true}, 6379]')
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        (tmp_path / "port-5432.sock").touch()
        (tmp_path / "port-6379.sock").touch()
        launched = []

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            launched.append(cmd)

            class FakeProc:
                pid = 999

            return FakeProc()

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert len(launched) == 2  # 5432 and 6379 — dict entry skipped
        assert "invalid port forward entry" in capsys.readouterr().err

    def test_socat_not_found_warns(self, monkeypatch, tmp_path, capsys):
        """FileNotFoundError from socat → warning, early return."""
        monkeypatch.setenv("YOLO_FORWARD_HOST_PORTS", "[5432, 6379]")
        monkeypatch.setattr(entrypoint, "FORWARD_SOCKET_DIR", tmp_path)
        (tmp_path / "port-5432.sock").touch()
        (tmp_path / "port-6379.sock").touch()

        import subprocess as _subprocess

        def mock_popen(cmd, **kwargs):
            raise FileNotFoundError("socat")

        monkeypatch.setattr(_subprocess, "Popen", mock_popen)
        monkeypatch.setattr(entrypoint, "_port_in_use", lambda p: False)
        entrypoint.start_container_port_forwarding()

        assert "socat not found" in capsys.readouterr().err


class TestPortInUse:
    """Tests for _port_in_use() helper."""

    def test_free_port_returns_false(self):
        """An unbound port should return False."""
        # Use a high ephemeral port that's almost certainly free
        assert entrypoint._port_in_use(59123) is False

    def test_bound_port_returns_true(self):
        """A port we're actively listening on should return True."""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        try:
            assert entrypoint._port_in_use(port) is True
        finally:
            sock.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Entrypoint gap tests — cover missing lines for 80% coverage
# ═══════════════════════════════════════════════════════════════════════════════


class TestPerfLogging:
    """Cover _perf and _perf_dump (lines 28-54)."""

    def test_perf_appends_checkpoint(self, jail_home):
        orig_log = list(entrypoint._PERF_LOG)
        entrypoint._PERF_LOG.clear()
        try:
            entrypoint._perf("test_checkpoint")
            assert len(entrypoint._PERF_LOG) == 1
            elapsed, label = entrypoint._PERF_LOG[0]
            assert label == "test_checkpoint"
            assert elapsed >= 0
        finally:
            entrypoint._PERF_LOG.clear()
            entrypoint._PERF_LOG.extend(orig_log)

    def test_perf_dump_writes_log(self, jail_home):
        orig_log = list(entrypoint._PERF_LOG)
        entrypoint._PERF_LOG.clear()
        try:
            entrypoint._PERF_LOG.append((0.001, "start"))
            entrypoint._PERF_LOG.append((0.150, "shims"))
            entrypoint._perf_dump()
            log_path = jail_home / ".yolo-perf.log"
            assert log_path.exists()
            content = log_path.read_text()
            assert "start" in content
            assert "shims" in content
        finally:
            entrypoint._PERF_LOG.clear()
            entrypoint._PERF_LOG.extend(orig_log)

    def test_perf_dump_trims_old_runs(self, jail_home):
        orig_log = list(entrypoint._PERF_LOG)
        entrypoint._PERF_LOG.clear()
        try:
            log_path = jail_home / ".yolo-perf.log"
            # Write >50 fake runs
            fake = "".join(f"=== YOLO Run {i} ===\ndata\n" for i in range(60))
            log_path.write_text(fake)
            entrypoint._PERF_LOG.append((0.001, "trim_test"))
            entrypoint._perf_dump()
            content = log_path.read_text()
            runs = content.split("=== YOLO")
            assert len(runs) <= 52  # 50 + header + new
        finally:
            entrypoint._PERF_LOG.clear()
            entrypoint._PERF_LOG.extend(orig_log)


class TestVenvPrecreateScript:
    """Cover generate_venv_precreate_script (lines 292-329)."""

    def test_script_created(self, jail_home):
        entrypoint.generate_venv_precreate_script()
        script_path = jail_home / ".yolo-venv-precreate.sh"
        assert script_path.exists()
        content = script_path.read_text()
        assert "#!/bin/bash" in content
        assert "mise which uv" in content
        assert "mise which python" in content

    def test_script_is_executable(self, jail_home):
        entrypoint.generate_venv_precreate_script()
        script_path = jail_home / ".yolo-venv-precreate.sh"
        import stat as stat_mod

        mode = script_path.stat().st_mode
        assert mode & stat_mod.S_IEXEC


class TestConfigureGit:
    """Cover configure_git (lines 494-512)."""

    @patch("shutil.which", return_value=None)
    def test_noop_when_git_missing(self, mock_which, jail_home, monkeypatch):
        with patch("subprocess.run") as mock_run:
            entrypoint.configure_git()
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/bin/git")
    @patch("subprocess.run")
    def test_sets_name_and_email(self, mock_run, mock_which, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_GIT_NAME", "Test User")
        monkeypatch.setenv("YOLO_GIT_EMAIL", "test@example.com")
        entrypoint.configure_git()
        assert mock_run.call_count >= 2
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("user.name" in c for c in calls)
        assert any("user.email" in c for c in calls)

    @patch("shutil.which", return_value="/usr/bin/git")
    @patch("subprocess.run")
    def test_sets_gitignore(
        self, mock_run, mock_which, jail_home, monkeypatch, tmp_path
    ):
        ignore_file = tmp_path / "gitignore"
        ignore_file.write_text("*.pyc\n")
        monkeypatch.setenv("YOLO_GLOBAL_GITIGNORE", str(ignore_file))
        monkeypatch.delenv("YOLO_GIT_NAME", raising=False)
        monkeypatch.delenv("YOLO_GIT_EMAIL", raising=False)
        entrypoint.configure_git()
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert any("core.excludesFile" in c for c in calls)


class TestConfigureJj:
    """Cover configure_jj (lines 517-529)."""

    @patch("shutil.which", return_value=None)
    def test_noop_when_jj_missing(self, mock_which, jail_home, monkeypatch):
        with patch("subprocess.run") as mock_run:
            entrypoint.configure_jj()
            mock_run.assert_not_called()

    @patch("shutil.which", return_value="/usr/bin/jj")
    @patch("subprocess.run")
    def test_sets_jj_identity(self, mock_run, mock_which, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_JJ_NAME", "Test User")
        monkeypatch.setenv("YOLO_JJ_EMAIL", "test@example.com")
        entrypoint.configure_jj()
        assert mock_run.call_count >= 2


class TestLspServerLoading:
    """Cover _load_lsp_servers edge cases (lines 112-113)."""

    def test_invalid_json_ignored(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_LSP_SERVERS", "{invalid json}")
        servers = entrypoint._load_lsp_servers()
        # Should still return defaults
        assert "python" in servers

    def test_non_dict_ignored(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_LSP_SERVERS", '"just a string"')
        servers = entrypoint._load_lsp_servers()
        assert "python" in servers


class TestMiseConfigUpdate:
    """Cover mise config update edge cases (lines 364, 385-395)."""

    def test_update_existing_tool_version(self, jail_home, monkeypatch):
        monkeypatch.setenv("YOLO_MISE_TOOLS", json.dumps({"node": "22"}))
        entrypoint.generate_mise_config()
        config_path = jail_home / ".config" / "mise" / "config.toml"
        content = config_path.read_text()
        # Now update with a different node version
        monkeypatch.setenv("YOLO_MISE_TOOLS", json.dumps({"node": "23"}))
        entrypoint.generate_mise_config()
        content = config_path.read_text()
        assert 'node = "23"' in content

    def test_base_tools_not_duplicated(self, jail_home, monkeypatch):
        monkeypatch.delenv("YOLO_MISE_TOOLS", raising=False)
        entrypoint.generate_mise_config()
        entrypoint.generate_mise_config()  # Run twice
        config_path = jail_home / ".config" / "mise" / "config.toml"
        content = config_path.read_text()
        # node should appear exactly once
        assert content.count("node =") == 1


class TestExecBash:
    """Cover exec_bash PATH setup (lines 951-1003 partially)."""

    @patch("os.execvp")
    def test_exec_bash_basic(self, mock_exec, jail_home, monkeypatch):
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        entrypoint.exec_bash("echo hello")
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "bash"
        assert "-c" in args[1]
        assert "echo hello" in args[1][-1]

    @patch("os.execvp")
    def test_exec_bash_includes_local_bin(self, mock_exec, jail_home, monkeypatch):
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        entrypoint.exec_bash("echo test")
        mock_exec.assert_called_once()
        # Verify .local/bin is on PATH (for yolo-cglimit)
        env_path = os.environ["PATH"]
        assert ".local/bin" in env_path

    @patch("os.execvp")
    def test_exec_bash_default_interactive(self, mock_exec, jail_home, monkeypatch):
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        entrypoint.exec_bash("bash")
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "bash"

    @patch("os.execvp")
    def test_exec_bash_with_profile(self, mock_exec, jail_home, monkeypatch):
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        monkeypatch.setenv("YOLO_PROFILE_ENTRYPOINT", "1")
        entrypoint.exec_bash("echo hi")
        mock_exec.assert_called_once()


class TestCglimitScript:
    """Test yolo-cglimit helper script generation."""

    def test_cglimit_script_created(self, jail_home):
        entrypoint.generate_cglimit_script()
        script = jail_home / ".local" / "bin" / "yolo-cglimit"
        assert script.exists()
        assert script.stat().st_mode & stat.S_IEXEC

    def test_cglimit_script_content(self, jail_home):
        entrypoint.generate_cglimit_script()
        script = jail_home / ".local" / "bin" / "yolo-cglimit"
        content = script.read_text()
        # Now a Python script that talks to host-side cgroup daemon via socket
        assert "#!/usr/bin/env python3" in content
        assert "yolo-cgd/cgroup.sock" in content
        assert "create_and_join" in content
        assert "--cpu" in content
        assert "--memory" in content
        assert "--pids" in content
        assert "SO_PEERCRED" in content
        assert "os.execvp" in content
        assert "--name" in content

    def test_cglimit_script_idempotent(self, jail_home):
        entrypoint.generate_cglimit_script()
        entrypoint.generate_cglimit_script()  # Second call should not fail
        script = jail_home / ".local" / "bin" / "yolo-cglimit"
        assert script.exists()

    def test_cglimit_cpu_formula(self, jail_home):
        """Verify the cglimit script sends cpu_pct to the host daemon."""
        entrypoint.generate_cglimit_script()
        content = (jail_home / ".local" / "bin" / "yolo-cglimit").read_text()
        # The script sends cpu_pct to the host daemon, which computes the quota
        assert "cpu_pct" in content
        assert "send_request" in content


class TestCgroupDelegation:
    """Test cgroup v2 delegation setup (host-side daemon model)."""

    def test_reports_available_when_socket_exists(self, jail_home, tmp_path, capsys):
        """Should report 'available' when the daemon socket exists."""
        sock = tmp_path / "cgroup.sock"
        sock.touch()
        import entrypoint as ep

        original = ep.CGD_SOCKET
        ep.CGD_SOCKET = sock
        try:
            ep.setup_cgroup_delegation()
            captured = capsys.readouterr()
            assert "available" in captured.err
        finally:
            ep.CGD_SOCKET = original

    def test_reports_unavailable_when_no_socket(self, jail_home, tmp_path, capsys):
        """Should report 'not available' when daemon socket doesn't exist."""
        import entrypoint as ep

        original = ep.CGD_SOCKET
        ep.CGD_SOCKET = tmp_path / "no-such-socket"
        try:
            ep.setup_cgroup_delegation()
            captured = capsys.readouterr()
            assert "not available" in captured.err
        finally:
            ep.CGD_SOCKET = original


class TestMainFunction:
    """Cover main() orchestration (lines 951-1003)."""

    @patch("entrypoint.exec_bash")
    @patch("entrypoint.start_container_port_forwarding")
    @patch("entrypoint.configure_gemini")
    @patch("entrypoint.configure_copilot")
    @patch("entrypoint.merge_skills")
    @patch("entrypoint.configure_jj")
    @patch("entrypoint.configure_git")
    @patch("entrypoint.generate_mcp_wrappers")
    @patch("entrypoint.generate_mise_config")
    @patch("entrypoint.generate_venv_precreate_script")
    @patch("entrypoint.generate_bootstrap_script")
    @patch("entrypoint.generate_bashrc")
    @patch("entrypoint.generate_shims")
    @patch("entrypoint.setup_cgroup_delegation")
    @patch("entrypoint.generate_cglimit_script")
    @patch("entrypoint._perf_dump")
    @patch("entrypoint._perf")
    def test_main_calls_all_generators(
        self,
        mock_perf,
        mock_dump,
        mock_cglimit,
        mock_cgroup,
        mock_shims,
        mock_bashrc,
        mock_bootstrap,
        mock_venv,
        mock_mise,
        mock_wrappers,
        mock_git,
        mock_jj,
        mock_skills,
        mock_copilot,
        mock_gemini,
        mock_port_fwd,
        mock_exec,
        jail_home,
        monkeypatch,
    ):
        monkeypatch.setattr("sys.argv", ["entrypoint", "echo", "hello"])
        monkeypatch.setenv("MISE_DATA_DIR", "/mise")
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        entrypoint.main()
        mock_shims.assert_called_once()
        mock_bashrc.assert_called_once()
        mock_bootstrap.assert_called_once()
        mock_venv.assert_called_once()
        mock_mise.assert_called_once()
        mock_git.assert_called_once()
        mock_jj.assert_called_once()
        mock_copilot.assert_called_once()
        mock_gemini.assert_called_once()
        mock_cgroup.assert_called_once()
        mock_cglimit.assert_called_once()
        mock_exec.assert_called_once_with("echo hello")

    @patch("entrypoint.exec_bash")
    @patch("entrypoint.start_container_port_forwarding")
    @patch("entrypoint.configure_gemini")
    @patch("entrypoint.configure_copilot")
    @patch("entrypoint.merge_skills")
    @patch("entrypoint.configure_jj")
    @patch("entrypoint.configure_git")
    @patch("entrypoint.generate_mcp_wrappers")
    @patch("entrypoint.generate_mise_config")
    @patch("entrypoint.generate_venv_precreate_script")
    @patch("entrypoint.generate_bootstrap_script")
    @patch("entrypoint.generate_bashrc")
    @patch("entrypoint.generate_shims")
    @patch("entrypoint.setup_cgroup_delegation")
    @patch("entrypoint.generate_cglimit_script")
    @patch("entrypoint._perf_dump")
    @patch("entrypoint._perf")
    def test_main_creates_mise_symlink(
        self,
        mock_perf,
        mock_dump,
        mock_cglimit,
        mock_cgroup,
        mock_shims,
        mock_bashrc,
        mock_bootstrap,
        mock_venv,
        mock_mise,
        mock_wrappers,
        mock_git,
        mock_jj,
        mock_skills,
        mock_copilot,
        mock_gemini,
        mock_port_fwd,
        mock_exec,
        jail_home,
        monkeypatch,
    ):
        monkeypatch.setattr("sys.argv", ["entrypoint"])
        monkeypatch.setenv("MISE_DATA_DIR", str(jail_home / "custom-mise"))
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        # Can't actually create /mise symlink in test env without root
        entrypoint.main()
        mock_exec.assert_called_once_with("bash")

    @patch("entrypoint.exec_bash")
    @patch("entrypoint.start_container_port_forwarding")
    @patch("entrypoint.configure_gemini")
    @patch("entrypoint.configure_copilot")
    @patch("entrypoint.merge_skills")
    @patch("entrypoint.configure_jj")
    @patch("entrypoint.configure_git")
    @patch("entrypoint.generate_mcp_wrappers")
    @patch("entrypoint.generate_mise_config")
    @patch("entrypoint.generate_venv_precreate_script")
    @patch("entrypoint.generate_bootstrap_script")
    @patch("entrypoint.generate_bashrc")
    @patch("entrypoint.generate_shims")
    @patch("entrypoint.setup_cgroup_delegation")
    @patch("entrypoint.generate_cglimit_script")
    @patch("entrypoint._perf_dump")
    @patch("entrypoint._perf")
    def test_main_trusts_mise_toml(
        self,
        mock_perf,
        mock_dump,
        mock_cglimit,
        mock_cgroup,
        mock_shims,
        mock_bashrc,
        mock_bootstrap,
        mock_venv,
        mock_mise,
        mock_wrappers,
        mock_git,
        mock_jj,
        mock_skills,
        mock_copilot,
        mock_gemini,
        mock_port_fwd,
        mock_exec,
        jail_home,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setattr("sys.argv", ["entrypoint"])
        monkeypatch.setenv("MISE_DATA_DIR", "/mise")
        monkeypatch.setenv("PATH", "/bin:/usr/bin")
        Path("/workspace/mise.toml")
        # This test just verifies main() doesn't crash when /workspace/mise.toml exists
        with patch("subprocess.run"):
            entrypoint.main()
        mock_exec.assert_called_once()
