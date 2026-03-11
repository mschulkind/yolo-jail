"""Unit tests for src/entrypoint.py config generation."""

import json
import os
import sys
from pathlib import Path

import pytest

# Import the entrypoint module
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import entrypoint


@pytest.fixture
def jail_home(tmp_path):
    """Redirect all entrypoint paths to a temp directory."""
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
        assert "alias copilot='copilot --yolo'" in content

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


# -- Copilot config --


class TestCopilotConfig:
    def test_mcp_config(self, jail_home):
        entrypoint.configure_copilot()
        mcp = json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
        assert "chrome-devtools" in mcp["mcpServers"]
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
    def test_fresh_config(self, jail_home):
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["security"]["approvalMode"] == "yolo"
        assert "chrome-devtools" in cfg["mcpServers"]
        assert "python-lsp" in cfg["mcpServers"]
        assert "typescript-lsp" in cfg["mcpServers"]
        assert "go-lsp" in cfg["mcpServers"]

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
        assert "chrome-devtools" in cfg["mcpServers"]

    def test_merge_preserves_existing_security(self, jail_home):
        entrypoint.GEMINI_DIR.mkdir(parents=True)
        existing = {"security": {"approvalMode": "confirm"}}
        (entrypoint.GEMINI_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_gemini()
        cfg = json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
        assert cfg["security"]["approvalMode"] == "confirm"

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

    def test_does_not_overwrite(self, jail_home):
        entrypoint.MISE_CONFIG_DIR.mkdir(parents=True)
        (entrypoint.MISE_CONFIG_DIR / "config.toml").write_text("custom")
        entrypoint.generate_mise_config()
        assert (entrypoint.MISE_CONFIG_DIR / "config.toml").read_text() == "custom"


# -- Bootstrap script --


class TestBootstrapScript:
    def test_creates_script(self, jail_home):
        entrypoint.generate_bootstrap_script()
        script = (jail_home / ".yolo-bootstrap.sh").read_text()
        assert "chrome-devtools-mcp" in script
        assert "mcp-language-server" in script
        assert "showboat" in script
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
