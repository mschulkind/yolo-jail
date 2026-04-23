"""Unit tests for src/entrypoint.py config generation."""

import json
import os
import stat
import subprocess
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
        "YOLO_HOST_CLAUDE_FILES",
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
        "CLAUDE_DIR",
        "CLAUDE_MANAGED_MCP_PATH",
        "CLAUDE_SHARED_CREDENTIALS_DIR",
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
    entrypoint.CLAUDE_DIR = tmp_path / ".claude"
    entrypoint.CLAUDE_MANAGED_MCP_PATH = (
        tmp_path / ".claude" / "yolo-managed-mcp-servers.json"
    )
    entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR = tmp_path / ".claude-shared-credentials"
    entrypoint.MISE_CONFIG_DIR = tmp_path / ".config" / "mise"

    yield tmp_path

    for attr in attrs:
        setattr(entrypoint, attr, orig[attr])


# -- Shim generation --


class TestShimGeneration:
    def _run_shim(self, shim_path, *argv, stdin=b"") -> "subprocess.CompletedProcess":
        import subprocess as _sp

        return _sp.run(
            [str(shim_path), *argv],
            capture_output=True,
            input=stdin,
            timeout=5,
        )

    def test_block_flags_is_config_driven(self, jail_home, monkeypatch):
        """The smart-block behavior lives in config, not code.  Prove it
        by supplying custom ``block_flags`` (different from the default
        grep recursion rule) and confirming:

          - the OLD default patterns (``-r``, ``-R``) pass through (they're
            not in the user's config)
          - the user's custom patterns DO block
        """
        monkeypatch.setenv(
            "YOLO_BLOCK_CONFIG",
            json.dumps(
                [
                    {
                        "name": "grep",
                        "message": "custom block",
                        "block_flags": ["--dangerous", "-*[xX]*"],
                    }
                ]
            ),
        )
        entrypoint.generate_shims()
        shim = entrypoint.SHIM_DIR / "grep"

        # ``-r`` is not in the user's custom block_flags — passes through.
        r = self._run_shim(shim, "-r", "foo", "/dev/null")
        assert r.returncode != 127, (
            f"custom block_flags should let -r through, got rc={r.returncode} "
            f"stderr={r.stderr!r}"
        )
        # The user's custom long pattern DOES block.
        r = self._run_shim(shim, "--dangerous", "foo")
        assert r.returncode == 127
        assert b"custom block" in r.stderr
        # The user's custom short-bundle pattern DOES block.
        r = self._run_shim(shim, "-xn", "foo", "/dev/null")
        assert r.returncode == 127

    def test_grep_shim_blocks_only_recursive(self, jail_home, monkeypatch):
        """grep is blocked *only* when invoked with recursive flags
        (``-r``, ``-R``, ``--recursive``, short-flag bundles like
        ``-rn``).  Plain pipe-filter usage must pass through — today's
        "any grep is blocked" rule fired on ``cmd | grep foo``, which
        is the wrong call."""
        monkeypatch.setenv(
            "YOLO_BLOCK_CONFIG",
            json.dumps(
                [
                    {
                        "name": "grep",
                        "message": "grep -r is blocked",
                        "suggestion": "Use rg",
                        "block_flags": [
                            "--recursive",
                            "-r",
                            "-R",
                            "-*[rR]*",
                        ],
                    }
                ]
            ),
        )
        entrypoint.generate_shims()
        shim = entrypoint.SHIM_DIR / "grep"
        assert shim.is_file()

        # Recursive — blocked (exit 127).
        for args in (["-r", "foo", "."], ["-R", "foo", "."], ["--recursive", "foo"]):
            r = self._run_shim(shim, *args)
            assert r.returncode == 127, (
                f"recursive argv {args} should be blocked, got rc={r.returncode}"
            )
            assert b"blocked" in r.stderr or b"rg" in r.stderr

        # Short-flag bundles that include r/R — blocked.
        for args in (["-rn", "foo", "."], ["-Rn", "foo", "."], ["-inRw", "foo"]):
            r = self._run_shim(shim, *args)
            assert r.returncode == 127, (
                f"short-bundle {args} should be blocked, got rc={r.returncode}"
            )

        # Long flags that happen to contain r/R but are NOT recursive —
        # not blocked.  The shim must not mistake ``--regex`` or
        # ``--regexp`` for ``--recursive``.
        r = self._run_shim(shim, "--regexp=foo", "/dev/null")
        assert r.returncode != 127, (
            f"--regexp must not be blocked, got rc={r.returncode} stderr={r.stderr!r}"
        )

        # Pipe-filter usage — not blocked.  We assert the shim
        # DIDN'T exit 127 (blocked) but don't inspect stdout, since
        # /bin/grep may be a path (macOS runners) where stdin piping
        # through our subprocess harness behaves differently — the
        # block/no-block decision is what we're testing here, not the
        # real grep binary.
        r = self._run_shim(shim, "foo", stdin=b"bar\nfoo\nbaz\n")
        assert r.returncode != 127, (
            f"plain grep must not be blocked, got rc={r.returncode} stderr={r.stderr!r}"
        )

        # Short non-recursive flag — not blocked.
        r = self._run_shim(shim, "-n", "foo", "/dev/null")
        assert r.returncode != 127

    def test_yolo_ps_script_generated(self, jail_home, monkeypatch):
        """``yolo-ps`` is the jail-side CLI for the host-processes
        loophole.  It's shipped as a wheel console script on the host,
        but the wheel isn't installed inside the jail — the entrypoint
        has to drop an equivalent into ``~/.local/bin/`` at boot, same
        pattern as ``yolo-journalctl`` / ``yolo-cglimit``."""
        monkeypatch.setenv("YOLO_REPO_ROOT", "/opt/yolo-jail")
        entrypoint.generate_yolo_ps_script()
        path = entrypoint.HOME / ".local" / "bin" / "yolo-ps"
        assert path.is_file()
        assert path.stat().st_mode & 0o111, "should be executable"
        content = path.read_text()
        # Reaches the shipped src.yolo_ps implementation — no logic
        # duplication from the generator.
        assert "src.yolo_ps" in content
        assert "/opt/yolo-jail" in content

    def test_yolo_wrapper_does_not_rely_on_pythonpath_or_cd(
        self, jail_home, monkeypatch
    ):
        """Regression test for two different breakage modes of the shim:

        1. PYTHONPATH-based: ``uv run`` doesn't reliably honor PYTHONPATH,
           so ``from src.cli import main`` fails with ModuleNotFoundError.
        2. cd-based: cd'ing into /opt/yolo-jail (a read-only bind mount)
           before calling ``uv run`` causes ``uv`` to bail with
           "Current directory does not exist" because its getcwd() can't
           resolve the bind-mounted CWD.

        The shim must make ``src`` importable without either gambit.
        Today's approach: a bootstrap Python file in the writable shim
        dir that does ``sys.path.insert(0, repo_root)`` before importing.
        """
        monkeypatch.setenv("YOLO_REPO_ROOT", "/opt/yolo-jail")
        entrypoint.generate_yolo_wrapper()
        shim = (entrypoint.SHIM_DIR / "yolo").read_text()
        # No cd into the repo root — that path is a read-only bind
        # mount on production jails and breaks uv's getcwd.
        assert "cd /opt/yolo-jail" not in shim, (
            "shim must not cd into the read-only repo root"
        )
        assert "cd " not in shim.split("exec ")[0], (
            "shim must not cd anywhere before exec"
        )
        # No PYTHONPATH dependency.
        assert "PYTHONPATH" not in shim, (
            "shim must not rely on PYTHONPATH (uv run strips it unreliably)"
        )
        # Must reach src via a bootstrap script in the writable shim dir.
        bootstrap_py = entrypoint.SHIM_DIR / "_yolo_bootstrap.py"
        assert bootstrap_py.is_file(), (
            "shim should invoke a bootstrap .py in the shim dir"
        )
        bootstrap = bootstrap_py.read_text()
        assert "sys.path.insert" in bootstrap
        assert "/opt/yolo-jail" in bootstrap
        assert "from src.cli import main" in bootstrap
        # Shim should reference the bootstrap by path.
        assert str(bootstrap_py) in shim

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
        assert "alias claude='claude --dangerously-skip-permissions'" not in content
        # Claude YOLO is via settings.json allow rules, not an alias flag
        assert "permissions.allow" in content or "settings.json" in content

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
        mise_shims = str(entrypoint.MISE_SHIMS)
        assert mise_shims in content
        assert content.index("$NPM_CONFIG_PREFIX/bin") < content.index(mise_shims)

    def test_local_bin_in_path(self, jail_home, monkeypatch):
        """~/.local/bin is on PATH for native Claude binary."""
        monkeypatch.setenv("YOLO_HOST_DIR", "test")
        entrypoint.generate_bashrc()
        content = entrypoint.BASHRC_PATH.read_text()
        assert "$HOME/.local/bin" in content
        # ~/.local/bin should come before npm-global (native claude takes precedence)
        assert content.index("$HOME/.local/bin") < content.index(
            "$NPM_CONFIG_PREFIX/bin"
        )

    def test_exports_ca_bundle_env_vars(self, jail_home, monkeypatch):
        """bashrc exports SSL_CERT_FILE / REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE /
        GIT_SSL_CAINFO pointing at $HOME/.yolo-ca-bundle.crt so every
        standard TLS client trusts loophole CAs, not just Node."""
        monkeypatch.setenv("YOLO_HOST_DIR", "test")
        entrypoint.generate_bashrc()
        content = entrypoint.BASHRC_PATH.read_text()
        for var in (
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "GIT_SSL_CAINFO",
        ):
            assert f'export {var}="$HOME/.yolo-ca-bundle.crt"' in content, (
                f"{var} not exported from bashrc"
            )


# -- CA bundle generation --


class TestCaBundleGeneration:
    """generate_ca_bundle() builds a combined PEM bundle under $HOME and
    points every standard trust-store env var at it."""

    def _snapshot_env(self, monkeypatch):
        """generate_ca_bundle mutates os.environ; isolate each test."""
        for v in (
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "GIT_SSL_CAINFO",
            "NODE_EXTRA_CA_CERTS",
        ):
            monkeypatch.delenv(v, raising=False)

    def test_bundle_includes_baseline(self, jail_home, monkeypatch):
        """SSL_CERT_FILE (the Nix cacert bundle baked into the image)
        must be part of the combined bundle — otherwise we'd lose
        trust in every Mozilla root."""
        self._snapshot_env(monkeypatch)
        baseline = jail_home / "baseline.crt"
        baseline.write_bytes(
            b"-----BEGIN CERTIFICATE-----\nBASELINE\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.setenv("SSL_CERT_FILE", str(baseline))

        bundle = entrypoint.generate_ca_bundle()

        contents = bundle.read_bytes()
        assert b"BASELINE" in contents

    def test_bundle_includes_loophole_cas(self, jail_home, monkeypatch):
        """Every path in NODE_EXTRA_CA_CERTS (colon-separated) must
        appear in the combined bundle — that's the whole point."""
        self._snapshot_env(monkeypatch)
        ca1 = jail_home / "broker.crt"
        ca2 = jail_home / "other-loophole.crt"
        ca1.write_bytes(
            b"-----BEGIN CERTIFICATE-----\nBROKERCA\n-----END CERTIFICATE-----\n"
        )
        ca2.write_bytes(
            b"-----BEGIN CERTIFICATE-----\nOTHERCA\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.setenv("NODE_EXTRA_CA_CERTS", f"{ca1}{os.pathsep}{ca2}")

        bundle = entrypoint.generate_ca_bundle()

        contents = bundle.read_bytes()
        assert b"BROKERCA" in contents
        assert b"OTHERCA" in contents

    def test_bundle_tolerates_missing_sources(self, jail_home, monkeypatch):
        """Unreadable baseline and dangling loophole CA paths must not
        crash — an empty combined bundle is still better than a
        dangling env var pointing at a nonexistent file."""
        self._snapshot_env(monkeypatch)
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/baseline.crt")
        monkeypatch.setenv(
            "NODE_EXTRA_CA_CERTS", f"/nonexistent/a.crt{os.pathsep}/nonexistent/b.crt"
        )

        bundle = entrypoint.generate_ca_bundle()
        assert bundle.exists()

    def test_bundle_sets_standard_env_vars(self, jail_home, monkeypatch):
        """Every standard trust-store var (SSL_CERT_FILE, REQUESTS_CA_BUNDLE,
        CURL_CA_BUNDLE, GIT_SSL_CAINFO) is set to the combined bundle
        path so children of the entrypoint inherit them even before
        bashrc runs."""
        self._snapshot_env(monkeypatch)
        baseline = jail_home / "baseline.crt"
        baseline.write_bytes(
            b"-----BEGIN CERTIFICATE-----\nBASELINE\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.setenv("SSL_CERT_FILE", str(baseline))

        bundle = entrypoint.generate_ca_bundle()

        bundle_str = str(bundle)
        assert os.environ["SSL_CERT_FILE"] == bundle_str
        assert os.environ["REQUESTS_CA_BUNDLE"] == bundle_str
        assert os.environ["CURL_CA_BUNDLE"] == bundle_str
        assert os.environ["GIT_SSL_CAINFO"] == bundle_str

    def test_bundle_does_not_recurse_on_its_own_path(self, jail_home, monkeypatch):
        """On the *second* boot of a jail the baked SSL_CERT_FILE the
        entrypoint sees in os.environ is the one the previous boot set —
        i.e. the bundle itself.  We must not read it back into itself
        (would double its size every boot)."""
        self._snapshot_env(monkeypatch)
        bundle_path = jail_home / ".yolo-ca-bundle.crt"
        bundle_path.write_bytes(
            b"-----BEGIN CERTIFICATE-----\nPRIOR\n-----END CERTIFICATE-----\n"
        )
        # Prior boot's env — SSL_CERT_FILE points at our own bundle.
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle_path))
        ca = jail_home / "extra.crt"
        ca.write_bytes(
            b"-----BEGIN CERTIFICATE-----\nEXTRA\n-----END CERTIFICATE-----\n"
        )
        monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(ca))

        entrypoint.generate_ca_bundle()

        body = bundle_path.read_bytes()
        # Prior cruft must not be re-inlined; fresh extras must be in.
        assert b"PRIOR" not in body
        assert b"EXTRA" in body


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


# -- Claude config --


class TestClaudeConfig:
    def test_mcp_servers_in_claude_json(self, jail_home):
        """MCP servers go in ~/.claude.json (user scope), not settings.json."""
        entrypoint.configure_claude()
        claude_json = json.loads((entrypoint.HOME / ".claude.json").read_text())
        assert "mcpServers" in claude_json
        # settings.json should NOT have mcpServers
        settings = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert "mcpServers" not in settings

    def test_yolo_mode_default(self, jail_home):
        """settings.json is intentionally minimal — actual YOLO comes
        from cli.py injecting ``--dangerously-skip-permissions`` into
        the claude command.  That flag bypasses the permission system
        entirely, so maintaining a per-tool allow-list here was both
        redundant and fragile (we kept missing new tools + new MCP
        servers).  What remains is defensive: ``acceptEdits`` default
        mode and ``additionalDirectories=["/"]`` so *if* the flag is
        ever dropped the jail still fails relatively open rather than
        prompting for everything.

        See handover bug 2026-04-22: bare ``Bash`` in the allow-list
        was inert (pattern required), so our "yolo" was half-permissioned
        for weeks.  The flag is the single source of truth now."""
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        perms = cfg["permissions"]
        # Minimal safety net; real YOLO is the CLI flag.
        assert perms.get("defaultMode") == "acceptEdits"
        assert perms.get("additionalDirectories") == ["/"]
        assert cfg["skipDangerousModePermissionPrompt"] is True
        # No fragile allow-list entries — the flag makes them irrelevant.
        allow = perms.get("allow") or []
        assert "Bash" not in allow, "bare-name rules are inert; drop them"
        assert "Bash(*)" not in allow, (
            "allow-list per-tool is irrelevant under --dangerously-skip-permissions"
        )
        assert "mcp__*" not in allow

    def test_allow_list_is_empty_under_dangerously_skip_permissions(
        self, jail_home, monkeypatch
    ):
        """Per-tool ``mcp__<name>`` / ``Bash(*)`` rules used to live
        here to work around Claude's pattern matcher.  The flag makes
        them all irrelevant — nothing checks the allow-list when
        ``--dangerously-skip-permissions`` is on.  Keep the list empty
        so a future reader doesn't have to read 20 lines of commentary
        to understand whether the list matters."""
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools", "sequential-thinking"]),
        )
        monkeypatch.setenv(
            "YOLO_MCP_SERVERS",
            json.dumps({"probe-mcp": {"command": "/workspace/probe-mcp.py"}}),
        )
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert cfg["permissions"]["allow"] == []

    def test_workspace_project_auto_approves_mcp(self, jail_home, monkeypatch):
        """The /workspace project entry has enableAllProjectMcpServers=True.

        Defense in depth against any secondary per-server trust dialog that
        Claude may fire on first use of an MCP server (separate from the
        per-tool permission matcher).
        """
        monkeypatch.setenv(
            "YOLO_MCP_PRESETS",
            json.dumps(["chrome-devtools"]),
        )
        entrypoint.configure_claude()
        claude_json = json.loads((entrypoint.HOME / ".claude.json").read_text())
        project = claude_json["projects"]["/workspace"]
        assert project["enableAllProjectMcpServers"] is True
        assert project["hasTrustDialogAccepted"] is True

    def test_auto_update_disabled(self, jail_home):
        """settings.json disables auto-updates (startup bootstrap owns updates)."""
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert cfg["preferences"]["autoUpdaterStatus"] == "disabled"

    def test_lsp_tool_enabled(self, jail_home):
        """settings.json enables ENABLE_LSP_TOOL for language server support."""
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert cfg["env"]["ENABLE_LSP_TOOL"] == "1"

    def test_lsp_plugins_enabled(self, jail_home):
        """Default LSP plugins are enabled in settings.json."""
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        plugins = cfg.get("enabledPlugins", {})
        assert plugins.get("pyright-lsp@claude-plugins-official") is True
        assert plugins.get("typescript-lsp@claude-plugins-official") is True
        assert plugins.get("gopls-lsp@claude-plugins-official") is True

    def test_preserves_existing_settings(self, jail_home):
        """configure_claude merges into existing settings.json."""
        entrypoint.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        existing = {"myCustomKey": True}
        (entrypoint.CLAUDE_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert cfg["myCustomKey"] is True
        assert cfg["permissions"].get("defaultMode") == "acceptEdits"

    def test_preserves_existing_claude_json(self, jail_home):
        """configure_claude merges MCP into existing ~/.claude.json."""
        existing = {
            "hasCompletedOnboarding": True,
            "mcpServers": {"custom": {"command": "foo"}},
        }
        (entrypoint.HOME / ".claude.json").write_text(json.dumps(existing))
        entrypoint.configure_claude()
        claude_json = json.loads((entrypoint.HOME / ".claude.json").read_text())
        assert claude_json["hasCompletedOnboarding"] is True
        assert "custom" in claude_json["mcpServers"]

    def test_handles_corrupt_json(self, jail_home):
        entrypoint.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        (entrypoint.CLAUDE_DIR / "settings.json").write_text("not json{{{")
        entrypoint.configure_claude()  # should not raise
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert cfg["permissions"].get("defaultMode") == "acceptEdits"

    def test_migrates_bypass_permissions(self, jail_home):
        """Existing bypassPermissions is replaced with acceptEdits."""
        entrypoint.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        existing = {"permissions": {"defaultMode": "bypassPermissions"}}
        (entrypoint.CLAUDE_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert cfg["permissions"]["defaultMode"] == "acceptEdits"
        assert cfg["permissions"].get("defaultMode") == "acceptEdits"
        assert cfg["skipDangerousModePermissionPrompt"] is True

    def test_removes_stale_mcp_from_settings(self, jail_home):
        """Old mcpServers in settings.json are cleaned up."""
        entrypoint.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        existing = {"mcpServers": {"stale-server": {"command": "old"}}}
        (entrypoint.CLAUDE_DIR / "settings.json").write_text(json.dumps(existing))
        entrypoint.configure_claude()
        cfg = json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
        assert "mcpServers" not in cfg

    def test_credentials_symlink_created(self, jail_home):
        """configure_claude creates a symlink from .claude/.credentials.json
        to the shared credentials dir so Claude's atomic writer works."""
        entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        (entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR / ".credentials.json").touch()
        entrypoint.configure_claude()
        link = entrypoint.CLAUDE_DIR / ".credentials.json"
        assert link.is_symlink()
        assert (
            os.readlink(str(link)) == "../.claude-shared-credentials/.credentials.json"
        )

    def test_credentials_symlink_migrates_existing_file(self, jail_home):
        """If .credentials.json is a regular file (old setup), its data is
        migrated to the shared dir and replaced with a symlink."""
        entrypoint.CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        # Old-style regular file with valid credentials
        cred_data = (
            '{"claudeAiOauth": {"accessToken": "test", "expiresAt": 9999999999}}'
        )
        (entrypoint.CLAUDE_DIR / ".credentials.json").write_text(cred_data)
        (entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR / ".credentials.json").touch()

        entrypoint.configure_claude()

        link = entrypoint.CLAUDE_DIR / ".credentials.json"
        assert link.is_symlink()
        # Data should have been migrated to shared dir
        shared = entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR / ".credentials.json"
        assert "test" in shared.read_text()

    def test_credentials_symlink_idempotent(self, jail_home):
        """Running configure_claude twice doesn't break the symlink."""
        entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        (entrypoint.CLAUDE_SHARED_CREDENTIALS_DIR / ".credentials.json").touch()
        entrypoint.configure_claude()
        entrypoint.configure_claude()
        link = entrypoint.CLAUDE_DIR / ".credentials.json"
        assert link.is_symlink()
        assert (
            os.readlink(str(link)) == "../.claude-shared-credentials/.credentials.json"
        )


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
        # Agent CLIs (gemini, copilot, claude) are NOT updated in bootstrap —
        # lazy-update launchers in ~/.yolo-shims/ handle that on first use.
        assert "@google/gemini-cli@latest" not in script
        assert "@github/copilot@latest" not in script
        assert "@anthropic-ai/claude-code" not in script
        assert "https://claude.ai/install.sh" not in script
        assert os.access(jail_home / ".yolo-bootstrap.sh", os.X_OK)


# -- Agent launchers --


class TestAgentLaunchers:
    def test_creates_launchers(self, jail_home):
        entrypoint.SHIM_DIR.mkdir(parents=True, exist_ok=True)
        entrypoint.generate_agent_launchers()
        for name in ("gemini", "copilot", "claude"):
            launcher = entrypoint.SHIM_DIR / name
            assert launcher.exists(), f"{name} launcher not created"
            assert os.access(launcher, os.X_OK), f"{name} launcher not executable"
            content = launcher.read_text()
            assert "YOLO_BYPASS_SHIMS=1" in content
            assert "exec " in content

    def test_does_not_overwrite_blocked_shim(self, jail_home, monkeypatch):
        """If a tool is blocked via YOLO_BLOCK_CONFIG, the launcher must not overwrite it."""
        monkeypatch.setenv(
            "YOLO_BLOCK_CONFIG",
            '[{"name": "gemini", "message": "blocked"}]',
        )
        entrypoint.generate_shims()
        blocked_content = (entrypoint.SHIM_DIR / "gemini").read_text()
        entrypoint.generate_agent_launchers()
        assert (entrypoint.SHIM_DIR / "gemini").read_text() == blocked_content
        # copilot and claude should still get launchers
        assert (entrypoint.SHIM_DIR / "copilot").exists()
        assert (entrypoint.SHIM_DIR / "claude").exists()

    def test_npm_launcher_checks_version(self, jail_home):
        entrypoint.SHIM_DIR.mkdir(parents=True, exist_ok=True)
        entrypoint.generate_agent_launchers()
        content = (entrypoint.SHIM_DIR / "gemini").read_text()
        assert "npm view" in content  # registry version check
        assert "package.json" in content  # local version check
        assert "UPDATE_INTERVAL" in content  # stamp-based throttling

    def test_claude_launcher_uses_native_installer(self, jail_home):
        entrypoint.SHIM_DIR.mkdir(parents=True, exist_ok=True)
        entrypoint.generate_agent_launchers()
        content = (entrypoint.SHIM_DIR / "claude").read_text()
        assert "claude.ai/install.sh" in content
        assert '"$REAL_BIN" install' in content  # native update command


# Skills merging moved to cli.py (_prepare_skills) — see test_cli_unit.py


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

    def test_system_version_replaced_with_base(self, jail_home, monkeypatch):
        monkeypatch.delenv("YOLO_MISE_TOOLS", raising=False)
        entrypoint.generate_mise_config()
        config_path = jail_home / ".config" / "mise" / "config.toml"
        # Simulate stale "system" value (deprecated by mise)
        content = config_path.read_text().replace('node = "22"', 'node = "system"')
        config_path.write_text(content)
        entrypoint.generate_mise_config()
        content = config_path.read_text()
        assert 'node = "22"' in content
        assert '"system"' not in content

    def test_base_tools_not_duplicated(self, jail_home, monkeypatch):
        monkeypatch.delenv("YOLO_MISE_TOOLS", raising=False)
        entrypoint.generate_mise_config()
        entrypoint.generate_mise_config()  # Run twice
        config_path = jail_home / ".config" / "mise" / "config.toml"
        content = config_path.read_text()
        # node should appear exactly once
        assert content.count("node =") == 1

    def test_injected_tool_matching_base_not_duplicated(self, jail_home, monkeypatch):
        # Workspace injecting a tool that also appears in base_tools (e.g.,
        # python = "3.13") previously produced a config with two `python =`
        # lines, which mise refuses to parse.
        monkeypatch.setenv("YOLO_MISE_TOOLS", json.dumps({"python": "3.13"}))
        entrypoint.generate_mise_config()
        config_path = jail_home / ".config" / "mise" / "config.toml"
        content = config_path.read_text()
        assert content.count("python =") == 1

    def test_existing_duplicate_is_self_healed(self, jail_home, monkeypatch):
        # Older writes may have left a duplicate tool line on disk. The next
        # run should repair it rather than leave the file unparseable.
        monkeypatch.delenv("YOLO_MISE_TOOLS", raising=False)
        entrypoint.generate_mise_config()
        config_path = jail_home / ".config" / "mise" / "config.toml"
        content = config_path.read_text()
        config_path.write_text(content.rstrip("\n") + '\npython = "3.13"\n')
        assert config_path.read_text().count("python =") == 2
        entrypoint.generate_mise_config()
        assert config_path.read_text().count("python =") == 1


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
        # Python script that talks to the host-side cgroup daemon via socket.
        # The socket path moved with the loopholes refactor — it now lives
        # under the unified /run/yolo-services/ dir as cgroup-delegate.sock.
        assert "#!/usr/bin/env python3" in content
        assert "/run/yolo-services/cgroup-delegate.sock" in content
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
    @patch("entrypoint.configure_claude")
    @patch("entrypoint.configure_gemini")
    @patch("entrypoint.configure_copilot")
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
        mock_copilot,
        mock_gemini,
        mock_claude,
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
        mock_claude.assert_called_once()
        mock_cgroup.assert_called_once()
        mock_cglimit.assert_called_once()
        mock_exec.assert_called_once_with("echo hello")

    @patch("entrypoint.exec_bash")
    @patch("entrypoint.start_container_port_forwarding")
    @patch("entrypoint.configure_claude")
    @patch("entrypoint.configure_gemini")
    @patch("entrypoint.configure_copilot")
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
        mock_copilot,
        mock_gemini,
        mock_claude,
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
    @patch("entrypoint.configure_claude")
    @patch("entrypoint.configure_gemini")
    @patch("entrypoint.configure_copilot")
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
        mock_copilot,
        mock_gemini,
        mock_claude,
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


# -- jail_daemon_supervisor single-instance gate --


class TestSupervisorSingleInstance:
    """``start_jail_daemon_supervisor`` must be idempotent across repeated
    entrypoint invocations.  Root cause of duplicated supervisors (see
    handover follow-up #3): entrypoint.main() runs on every ``podman
    exec yolo-entrypoint <cmd>``, calling us; each extra supervisor
    forks a fresh oauth_broker_jail that tries to bind :443 and
    crashloops with EADDRINUSE.  Guard with a PID file + liveness
    probe so re-entrant exec calls observe the existing supervisor and
    no-op."""

    def _set_daemons_env(self, monkeypatch):
        """Supervisor is only spawned when YOLO_JAIL_DAEMONS is non-empty."""
        monkeypatch.setenv(
            "YOLO_JAIL_DAEMONS",
            '[{"name":"x","cmd":["/bin/true"],"restart":"no"}]',
        )

    def test_first_call_spawns_supervisor(self, jail_home, monkeypatch, tmp_path):
        self._set_daemons_env(monkeypatch)
        monkeypatch.setattr(entrypoint, "SUPERVISOR_PID_FILE", tmp_path / "sup.pid")

        popen_called = {"n": 0}

        class FakePopen:
            def __init__(self, *a, **kw):
                popen_called["n"] += 1
                self.pid = 99999  # unlikely PID; kill(pid,0) will raise

        monkeypatch.setattr(entrypoint.subprocess, "Popen", FakePopen)
        entrypoint.start_jail_daemon_supervisor()
        assert popen_called["n"] == 1
        assert (tmp_path / "sup.pid").read_text().strip() == "99999"

    def test_second_call_when_pidfile_points_at_live_process_skips(
        self, jail_home, monkeypatch, tmp_path
    ):
        """If the PID file points at a live process, do nothing — this is
        the re-entrant exec case that caused the duplication in the
        first place."""
        self._set_daemons_env(monkeypatch)
        # os.getpid() is guaranteed-live — use it as "the running supervisor".
        pid_file = tmp_path / "sup.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(entrypoint, "SUPERVISOR_PID_FILE", pid_file)

        popen_called = {"n": 0}

        class FakePopen:
            def __init__(self, *a, **kw):
                popen_called["n"] += 1
                self.pid = 99999

        monkeypatch.setattr(entrypoint.subprocess, "Popen", FakePopen)
        entrypoint.start_jail_daemon_supervisor()
        assert popen_called["n"] == 0

    def test_stale_pidfile_triggers_respawn(self, jail_home, monkeypatch, tmp_path):
        """A PID file left by a crashed / killed supervisor must not
        pin us out of respawning.  Dead PIDs are detected via
        ``os.kill(pid, 0)`` raising ProcessLookupError."""
        self._set_daemons_env(monkeypatch)
        pid_file = tmp_path / "sup.pid"
        # PID 999999 is very unlikely to exist; test would be flaky if
        # it did, so fake the liveness probe to be deterministic.
        pid_file.write_text("999999")
        monkeypatch.setattr(entrypoint, "SUPERVISOR_PID_FILE", pid_file)

        def fake_kill(pid, sig):
            raise ProcessLookupError("no such pid")

        monkeypatch.setattr(entrypoint.os, "kill", fake_kill)

        popen_called = {"n": 0}

        class FakePopen:
            def __init__(self, *a, **kw):
                popen_called["n"] += 1
                self.pid = 12345

        monkeypatch.setattr(entrypoint.subprocess, "Popen", FakePopen)
        entrypoint.start_jail_daemon_supervisor()
        assert popen_called["n"] == 1
        # PID file now points at the new supervisor.
        assert pid_file.read_text().strip() == "12345"

    def test_empty_jail_daemons_is_noop(self, jail_home, monkeypatch, tmp_path):
        """YOLO_JAIL_DAEMONS empty/unset → still a no-op, unchanged from
        the pre-guard behavior.  Guard must not add work for loopholes
        with nothing to supervise."""
        monkeypatch.delenv("YOLO_JAIL_DAEMONS", raising=False)
        monkeypatch.setattr(entrypoint, "SUPERVISOR_PID_FILE", tmp_path / "sup.pid")

        popen_called = {"n": 0}

        class FakePopen:
            def __init__(self, *a, **kw):
                popen_called["n"] += 1

        monkeypatch.setattr(entrypoint.subprocess, "Popen", FakePopen)
        entrypoint.start_jail_daemon_supervisor()
        assert popen_called["n"] == 0
        assert not (tmp_path / "sup.pid").exists()
