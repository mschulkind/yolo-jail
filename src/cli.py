import difflib
import fcntl
import os
import re
import subprocess
import sys
import json
import shlex
import shutil
import hashlib
import time
import tempfile
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import typer
import pyjson5
from rich.console import Console

app = typer.Typer(
    invoke_without_command=True,
    rich_markup_mode="rich",
    no_args_is_help=False,
)


@app.callback()
def _default(ctx: typer.Context):
    """[bold]YOLO Jail[/bold] — Secure container environment for AI agents.

    Runs AI agents (Copilot, Gemini CLI) in isolated Docker/Podman containers
    with no access to host credentials (~/.ssh, ~/.gitconfig, cloud tokens).
    Tool state persists across restarts.

    [bold cyan]Quick Start[/bold cyan]

        yolo                      Interactive jail shell
        yolo -- copilot           Run Copilot in jail (--yolo auto-injected)
        yolo -- gemini            Run Gemini in jail (--yolo auto-injected)
        yolo --new -- bash        Force new container (ignore running one)
        yolo --profile -- echo hi Profile startup performance
        yolo check                Validate config and preflight the build
        yolo ps                   List running jails
        yolo init                 Create config + agent briefing
        yolo config-ref           Full configuration reference

    [bold cyan]What Agents Get Inside the Jail[/bold cyan]

        Workspace:  Your project is bind-mounted at /workspace (read-write,
                    same files — edits are visible on the host immediately)
        Internet:   Full network access (bridge mode by default)
        Tools:      Node.js 22, Python 3.13, Go, rg, fd, bat, jq, git, gh,
                    nvim, curl, strace, and anything in packages/mise_tools
        Home:       /home/agent — shared across ALL jails. Auth tokens,
                    tool caches, and configs persist across restarts.
        Identity:   Host git/jj identity is injected automatically.
                    GitHub CLI (gh) is pre-authenticated.

        NOT shared: ~/.ssh, ~/.gitconfig, cloud credentials, host PATH.
        Blocked:    grep → rg, find → fd (configurable). Set YOLO_BYPASS_SHIMS=1
                    in scripts that need the originals.

    [bold cyan]Configuration[/bold cyan]

    Place [bold]yolo-jail.jsonc[/bold] in your project root (JSON with comments):

        {
          "runtime": "podman",              // or "docker"
          "packages": [                     // extra nix packages
            "strace",                       // latest from flake nixpkgs
            {"name": "freetype", "nixpkgs": "e6f23dc0..."},  // pinned nixpkgs
            {"name": "freetype", "version": "2.14.1",        // version override
             "url": "mirror://savannah/freetype/freetype-2.14.1.tar.xz",
             "hash": "sha256-..."}
          ],
          "mounts": ["/path/to/repo"],      // read-only at /ctx/<name>
          "network": {"mode": "bridge", "ports": ["8000:8000"]},
          "security": {"blocked_tools": ["curl", "wget"]}
        }

    User defaults: ~/.config/yolo-jail/config.jsonc (merged under workspace).
    Run [bold]yolo check[/bold] to validate config changes before restarting.
    Run [bold]yolo config-ref[/bold] for the complete field reference.

    [bold cyan]Environment Variables[/bold cyan]

        YOLO_RUNTIME          Override runtime (podman/docker)
        YOLO_BYPASS_SHIMS     Set to 1 to bypass blocked tool shims

    [bold cyan]Config Safety[/bold cyan]

    When yolo-jail.jsonc changes between runs, the CLI shows a diff and asks
    for human confirmation before starting. This prevents agents from silently
    modifying the config without the operator noticing.

    [bold cyan]Agent Package Workflow[/bold cyan]

    Agents inside the jail can edit yolo-jail.jsonc to add packages, but they
    MUST run [bold]yolo check[/bold] after every config edit before asking the human
    to restart. The human sees the diff and approves at next startup.
    Use [bold]yolo check --no-build[/bold] inside a running jail for a quick preflight.
    See [bold]yolo config-ref[/bold] for details.
    """
    if ctx.invoked_subcommand is None:
        # No subcommand → default to `run` (interactive shell)
        ctx.invoke(run)


JAIL_IMAGE = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"
CONTAINER_DIR = GLOBAL_STORAGE / "containers"
AGENTS_DIR = GLOBAL_STORAGE / "agents"
BUILD_DIR = GLOBAL_STORAGE / "build"
USER_CONFIG_PATH = Path.home() / ".config" / "yolo-jail" / "config.jsonc"

console = Console()


class ConfigError(ValueError):
    """Raised when a yolo-jail config file or merged config is invalid."""


def _resolve_repo_root() -> Path:
    """Find the yolo-jail repo root for nix image builds.

    Resolution order:
      1. YOLO_REPO_ROOT env var (set inside jails and CI)
      2. Source checkout detection (Path(__file__) → parent → flake.nix exists)
      3. User config repo_path field (~/.config/yolo-jail/config.jsonc)
      4. Error with helpful message
    """
    # 1. Env var (used inside jails, CI, etc.)
    env_val = os.environ.get("YOLO_REPO_ROOT")
    if env_val:
        return Path(env_val).resolve()

    # 2. Running from source checkout (dev mode)
    source_root = Path(__file__).parent.parent
    if (source_root / "flake.nix").exists():
        return source_root.resolve()

    # 3. User config
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH) as f:
                cfg = pyjson5.load(f)
            repo_path = cfg.get("repo_path")
            if repo_path:
                p = Path(repo_path).expanduser().resolve()
                if (p / "flake.nix").exists():
                    return p
        except Exception:
            pass

    console.print(
        "[bold red]Cannot find yolo-jail repo root.[/bold red]\n"
        "The yolo CLI needs the repo for nix image builds.\n\n"
        "Fix: add [bold]repo_path[/bold] to ~/.config/yolo-jail/config.jsonc:\n"
        '  { "repo_path": "~/code/yolo_jail" }'
    )
    raise typer.Exit(1)


def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)
    CONTAINER_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # Pre-create intermediate directories inside GLOBAL_HOME that will be parents
    # of nested bind mounts.  Without this, Docker daemon (running as root) auto-creates
    # them as root:root, making them unwritable by the -u UID:GID container process.
    # Podman rootless is unaffected (UID mapping handles ownership).
    for subdir in [".copilot", ".gemini", Path(".config") / "git"]:
        (GLOBAL_HOME / subdir).mkdir(parents=True, exist_ok=True)


def _get_project_name() -> str:
    """Return the jail project label: SM_PROJECT if set, else cwd basename."""
    return os.environ.get("SM_PROJECT") or Path.cwd().name


def _tmux_rename_window(name: str):
    """Rename the current tmux window. No-op if not in tmux or YOLO_NO_TMUX=1."""
    if os.environ.get("YOLO_NO_TMUX") == "1":
        return
    if os.environ.get("TMUX"):
        try:
            subprocess.run(
                ["tmux", "rename-window", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def _kitty_setup_jail_tab():
    """Set kitty tab title and color for jail indicator. Returns cleanup function or None."""
    if not os.environ.get("KITTY_PID") or not sys.stdin.isatty():
        return None

    project = _get_project_name()
    window_id = os.environ.get("KITTY_WINDOW_ID", "")
    match_arg = f"id:{window_id}" if window_id else "recent:0"

    def _kitten_run(cmd_args):
        try:
            subprocess.run(
                ["kitten", "@", *cmd_args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    try:
        old_title = (
            subprocess.check_output(
                ["kitten", "@", "get-tab-title", "--match", match_arg],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        old_title = ""

    try:
        subprocess.run(
            [
                "kitten",
                "@",
                "set-tab-title",
                "--match",
                match_arg,
                f"🔒 JAIL {project}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    # Turn the tab red
    _kitten_run(
        [
            "set-tab-color",
            "--match",
            match_arg,
            "active_bg=#cc0000",
            "active_fg=#ffffff",
            "inactive_bg=#880000",
            "inactive_fg=#cccccc",
        ]
    )

    def restore():
        _kitten_run(["set-tab-title", "--match", match_arg, old_title or "bash"])
        # Reset tab colors to kitty.conf defaults
        _kitten_run(
            [
                "set-tab-color",
                "--match",
                match_arg,
                "active_bg=none",
                "active_fg=none",
                "inactive_bg=none",
                "inactive_fg=none",
            ]
        )

    return restore


def _tmux_setup_jail_pane():
    """Set tmux pane border indicators for the jail. Returns cleanup function."""
    if os.environ.get("YOLO_NO_TMUX") == "1":
        return None
    if not os.environ.get("TMUX") or not sys.stdin.isatty():
        return None

    pane = os.environ.get("TMUX_PANE", "")
    jail_dir = _get_project_name()

    def _tmux_opt(opt):
        try:
            r = subprocess.run(
                ["tmux", "show-option", "-pt", pane, opt],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                # Output is "option-name value" — extract value after first space
                parts = r.stdout.strip().split(None, 1)
                return parts[1] if len(parts) > 1 else ""
            return None
        except Exception:
            return None

    def _tmux_set(opt, val):
        try:
            subprocess.run(
                ["tmux", "set-option", "-pt", pane, opt, val],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _tmux_unset(opt):
        try:
            subprocess.run(
                ["tmux", "set-option", "-put", pane, opt],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # Save old state
    old = {
        opt: _tmux_opt(opt)
        for opt in [
            "pane-border-style",
            "pane-active-border-style",
            "pane-border-status",
            "pane-border-format",
        ]
    }
    old_window = None
    old_auto_rename = None
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        old_window = r.stdout.strip() if r.returncode == 0 else None
        r = subprocess.run(
            ["tmux", "show-window-option", "-v", "automatic-rename"],
            capture_output=True,
            text=True,
        )
        old_auto_rename = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        pass

    # Set jail indicators
    _tmux_set("pane-border-style", "fg=red,bold")
    _tmux_set("pane-active-border-style", "fg=red,bold")
    _tmux_set("pane-border-status", "bottom")
    _tmux_set("pane-border-format", f" 🔒 JAIL {jail_dir} ")
    try:
        subprocess.run(
            ["tmux", "set-window-option", "automatic-rename", "off"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["tmux", "rename-window", "JAIL"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    def restore():
        # Batch all tmux restores into a single command to minimize shutdown delay
        cmds = []
        for opt, val in old.items():
            if val is not None:
                cmds.append(f"set-option -pt {pane} {opt} {val}")
            else:
                cmds.append(f"set-option -put {pane} {opt}")
        if old_window:
            cmds.append(f"rename-window {old_window}")
        if old_auto_rename == "on":
            cmds.append("set-window-option automatic-rename on")
        if cmds:
            try:
                # Execute all restores in one tmux invocation using \;
                full_cmd = ["tmux"]
                for i, cmd in enumerate(cmds):
                    if i > 0:
                        full_cmd.append(";")
                    full_cmd.extend(cmd.split())
                subprocess.run(
                    full_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except Exception:
                pass

    return restore


def _runtime(config: Dict[str, Any] = None) -> str:
    """Return container runtime: 'podman' or 'docker'."""
    env = os.environ.get("YOLO_RUNTIME")
    if env and env in ("podman", "docker"):
        return env
    if config:
        cfg = config.get("runtime")
        if cfg and cfg in ("podman", "docker"):
            return cfg
    for rt in ("podman", "docker"):
        if shutil.which(rt):
            return rt
    console.print(
        "[bold red]No container runtime found. Install podman or docker.[/bold red]"
    )
    sys.exit(1)


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic container name from workspace path."""
    h = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:12]
    return f"yolo-{h}"


def find_running_container(name: str, runtime: str = "docker") -> Optional[str]:
    """Return container ID if a container with this name is running, else None."""
    try:
        result = subprocess.run(
            [runtime, "ps", "-q", "--filter", f"name=^/{name}$"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    cid = result.stdout.strip()
    return cid if cid else None


def write_container_tracking(name: str, workspace: Path):
    """Write a tracking file so users can inspect active containers."""
    tracking_file = CONTAINER_DIR / name
    tracking_file.write_text(str(workspace.resolve()) + "\n")


def cleanup_container_tracking(name: str):
    """Remove tracking file for a container."""
    tracking_file = CONTAINER_DIR / name
    tracking_file.unlink(missing_ok=True)


def _parse_port_forwards(forward_host_ports: List) -> List[tuple]:
    """Parse forward_host_ports config into (local_port, host_port) tuples."""
    result = []
    for entry in forward_host_ports:
        if isinstance(entry, int):
            result.append((entry, entry))
        elif isinstance(entry, str) and ":" in entry:
            parts = entry.split(":", 1)
            result.append((int(parts[0]), int(parts[1])))
        elif isinstance(entry, str):
            port = int(entry)
            result.append((port, port))
        else:
            print(f"Warning: invalid port forward entry: {entry}", file=sys.stderr)
    return result


def start_host_port_forwarding(
    forward_host_ports: List, cname: str, socket_dir: Path
) -> List[subprocess.Popen]:
    """Start host-side socat to bridge Unix sockets to host localhost services.

    Uses Unix sockets (shared via bind mount) to tunnel host localhost ports
    into the jail — analogous to SSH -L port forwarding. This avoids exposing
    services to the network and works regardless of container networking mode
    (pasta, slirp4netns, bridge, etc.).

    Architecture:
      container app → container socat (TCP→Unix) → socket file → host socat (Unix→TCP) → host 127.0.0.1

    Host side (this function): socat UNIX-LISTEN:sock → TCP:127.0.0.1:PORT
    Container side (entrypoint.py): socat TCP-LISTEN:PORT → UNIX-CONNECT:sock

    Must be called BEFORE the container starts so socket files exist when
    entrypoint.py runs.
    """
    if not forward_host_ports:
        return []

    parsed = _parse_port_forwards(forward_host_ports)
    if not parsed:
        return []

    socket_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path.home() / ".local" / "share" / "yolo-jail" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"{cname}-socat.log", "a")

    processes = []
    for local_port, host_port in parsed:
        sock_path = socket_dir / f"port-{local_port}.sock"
        # Remove stale socket from previous run
        sock_path.unlink(missing_ok=True)

        try:
            proc = subprocess.Popen(
                [
                    "socat",
                    f"UNIX-LISTEN:{sock_path},fork,mode=777",
                    f"TCP:127.0.0.1:{host_port}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=log_file,
            )
            processes.append(proc)
        except FileNotFoundError:
            print(
                "Warning: socat not found on host, cannot forward ports. "
                "Install socat (e.g., nix-shell -p socat, apt install socat).",
                file=sys.stderr,
            )
            break
        except Exception as e:
            print(
                f"Warning: failed to start port forward {local_port}: {e}",
                file=sys.stderr,
            )

    # Give socat a moment to create the socket files before the container starts
    if processes:
        time.sleep(0.1)

    return processes


def cleanup_port_forwarding(
    socat_procs: List[subprocess.Popen], socket_dir: Optional[Path]
):
    """Terminate host-side socat processes and remove socket directory."""
    for sp in socat_procs:
        try:
            sp.terminate()
            sp.wait(timeout=2)
        except Exception:
            try:
                sp.kill()
            except Exception:
                pass
    if socket_dir and socket_dir.exists():
        shutil.rmtree(socket_dir, ignore_errors=True)


DEFAULT_MCP_SERVER_NAMES = ["chrome-devtools", "sequential-thinking"]
DEFAULT_MISE_TOOLS = {"neovim": "stable"}


def _effective_mcp_server_names(
    mcp_servers: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return the effective MCP server names after config overrides/removals."""
    names = list(DEFAULT_MCP_SERVER_NAMES)
    if not isinstance(mcp_servers, dict):
        return names

    for name, cfg in mcp_servers.items():
        if cfg is None:
            if name in names:
                names.remove(name)
            continue
        if isinstance(cfg, dict) and name not in names:
            names.append(name)
    return names


def _merge_mise_tools(config: Dict[str, Any]) -> Dict[str, Any]:
    """Merge built-in mise defaults with config overrides."""
    return {**DEFAULT_MISE_TOOLS, **config.get("mise_tools", {})}


def _normalize_blocked_tools(
    security_section: Optional[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Normalize blocked tool config into the format consumed by the entrypoint."""
    if security_section is None:
        security_section = {}

    raw_blocked = security_section.get("blocked_tools", ["grep", "find"])
    if raw_blocked is None:
        raw_blocked = ["grep", "find"]

    default_messages = {
        "grep": {
            "message": "grep is blocked to prevent unintended recursive searches. Use ripgrep (rg) or other targeted tools.",
            "suggestion": "Try: rg <pattern> [file]",
        },
        "find": {
            "message": "find is blocked to prevent unintended recursive searches. Use fd for a faster, more intuitive alternative.",
            "suggestion": "Try: fd <pattern>",
        },
    }

    normalized_blocked = []
    for tool in raw_blocked:
        if isinstance(tool, str):
            tool_dict = {"name": tool}
            if tool in default_messages:
                tool_dict.update(default_messages[tool])
            normalized_blocked.append(tool_dict)
        elif isinstance(tool, dict) and "name" in tool:
            normalized_blocked.append(tool)
    return normalized_blocked


def _host_mise_dir() -> Path:
    """Return the host-visible mise data dir shared with the jail."""
    host_mise_path = os.environ.get("YOLO_OUTER_MISE_PATH") or os.environ.get(
        "MISE_DATA_DIR", str(Path.home() / ".local" / "share" / "mise")
    )
    host_mise = Path(host_mise_path)
    if not host_mise.exists():
        host_mise.mkdir(parents=True, exist_ok=True)
    return host_mise


def generate_agents_md(
    cname: str,
    workspace: Path,
    blocked_tools: List[Dict[str, str]],
    mount_descriptions: List[str],
    net_mode: str = "bridge",
    runtime: str = "podman",
    forward_host_ports: Optional[List] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
) -> Path:
    """Generate per-workspace AGENTS.md files and return the directory.

    Produces separate files for Copilot and Gemini so that user-level
    AGENTS.md content from ~/.copilot/AGENTS.md and ~/.gemini/AGENTS.md
    can differ between the two agents.
    """
    agents_dir = AGENTS_DIR / cname
    agents_dir.mkdir(parents=True, exist_ok=True)

    if net_mode == "host":
        network_line = "- **Network**: Host networking — the container shares the host network stack. `localhost` / `127.0.0.1` resolves directly to the host. No port mapping needed."
    elif runtime == "podman":
        network_line = "- **Network**: Bridge mode. Use `host.containers.internal` (resolves to 169.254.1.2) to reach the host."
    else:  # docker bridge
        network_line = "- **Network**: Bridge mode (Docker). Use `host.internal` to reach the host."

    # Build forwarded host ports description
    forwarded_ports_lines = []
    if forward_host_ports and net_mode != "host":
        forwarded_ports_lines.append(
            "- **Forwarded Host Ports**: The following host services are available on `localhost` inside this container:"
        )
        for entry in forward_host_ports:
            if isinstance(entry, int):
                forwarded_ports_lines.append(
                    f"  - `localhost:{entry}` → host port {entry}"
                )
            elif isinstance(entry, str) and ":" in entry:
                parts = entry.split(":", 1)
                forwarded_ports_lines.append(
                    f"  - `localhost:{parts[0]}` → host port {parts[1]}"
                )
            elif isinstance(entry, str):
                forwarded_ports_lines.append(
                    f"  - `localhost:{entry}` → host port {entry}"
                )

    mcp_server_names = _effective_mcp_server_names(mcp_servers)

    lines = [
        "# YOLO Jail Environment",
        "",
        "You are running inside a YOLO Jail — a sandboxed Docker container.",
        "",
        "## Environment",
        "",
        f"- **Workspace**: `/workspace` (mounted from host `{workspace}`)",
        "- **Home Directory**: `/home/agent` (persistent across sessions)",
        "- **OS**: NixOS-based minimal container (no systemd, no sudo)",
        network_line,
        *forwarded_ports_lines,
        "",
        "## Available Tools",
        "",
        "Standard CLI tools: git, rg (ripgrep), fd, bat, jq, nvim, curl, wget, strace, gh",
        "Runtimes: Node.js 22, Python 3.13, Go (managed by mise)",
        f"MCP Servers: {', '.join(mcp_server_names)}",
        "",
    ]

    if blocked_tools:
        lines.append("## Blocked Tools")
        lines.append("")
        lines.append("The following tools are blocked or shimmed in this project:")
        lines.append("")
        for tool in blocked_tools:
            name = tool.get("name", str(tool))
            msg = tool.get("message", "")
            sug = tool.get("suggestion", "")
            entry = f"- `{name}`"
            if msg:
                entry += f": {msg}"
            if sug:
                entry += f" Use `{sug}` instead."
            lines.append(entry)
        lines.append("")

    if mount_descriptions:
        lines.append("## Additional Context Mounts (read-only)")
        lines.append("")
        for m in mount_descriptions:
            host_path, container_path = m.split(":", 1) if ":" in m else (m, m)
            lines.append(f"- `{container_path}` (from host `{host_path}`)")
        lines.append("")

    lines.extend(
        [
            "## Limitations",
            "",
            "- **No internet restrictions** but no host credentials (no ~/.ssh, no ~/.gitconfig).",
            "- **No pagers**: PAGER=cat, GIT_PAGER=cat. Do not pipe to less/more.",
            "- **Read-only mounts**: Context mounts under `/ctx/` are read-only.",
            "- **No sudo/root**: You run as a mapped host user with no privilege escalation.",
            "- **No git push/pull**: No GitHub credentials are available. Do not attempt `gh auth login` or SSH-based git operations.",
            "",
            "## Adding Packages",
            "",
            "If you need a tool that is not installed, you can request it:",
            "",
            "1. Edit `/workspace/yolo-jail.jsonc` and add the package to the `packages` array",
            "2. ALWAYS run `yolo check` after every config edit (`yolo check --no-build` is fine inside a running jail)",
            '3. If the check passes, tell the human user: "Please restart the jail so the new package becomes available"',
            "4. The human will see a config diff and confirm the change at next startup",
            "5. After restart, the package will be available",
            "",
            "Example — to add PostgreSQL tools (latest version):",
            "```json",
            '  "packages": ["postgresql"]',
            "```",
            "",
            "To pin a specific version, use an object with a nixpkgs commit hash:",
            "```json",
            '  "packages": [{"name": "freetype", "nixpkgs": "e6f23dc0..."}]',
            "```",
            "Find nixpkgs commits for specific versions at: https://lazamar.co.uk/nix-versions/",
            "",
            "To override a version with an upstream source (when nixpkgs hasn't caught up):",
            "```json",
            '  "packages": [{"name": "freetype", "version": "2.14.1",',
            '    "url": "mirror://savannah/freetype/freetype-2.14.1.tar.xz",',
            '    "hash": "sha256-MkJ+jEcawJWFMhKjeu+BbGC0IFLU2eSCMLqzvfKTbMw="}]',
            "```",
            "Get the hash: run nix-prefetch-url <url>, or set hash to empty and nix reports it.",
            "",
            "Package names must match nixpkgs attributes (https://search.nixos.org/packages).",
            "Do NOT install packages via apt, nix-env, or other package managers.",
            "Run `yolo config-ref` for the full configuration reference.",
            "",
            "## First Session — Handover",
            "",
            "If this is your first session in this jail, invoke the **jail-startup** skill.",
            "It reads the handover document at `.yolo/handover.md` left by the outer agent",
            "and orients you to the jail environment. The human may ask you to invoke it —",
            'just say "invoke the jail-startup skill" or use your skill invocation tool.',
            "",
        ]
    )

    jail_content = "\n".join(lines) + "\n"

    home = Path.home()
    for agent, dotdir in [("copilot", ".copilot"), ("gemini", ".gemini")]:
        user_agents = home / dotdir / "AGENTS.md"
        if user_agents.exists():
            user_content = user_agents.read_text()
            content = user_content + "\n---\n\n" + jail_content
        else:
            content = jail_content
        (agents_dir / f"AGENTS-{agent}.md").write_text(content)

    return agents_dir


def _summarize_nix_line(line: str) -> str:
    """Extract a short human-readable summary from nix build stderr."""
    # "copying path '/nix/store/hash-name-1.0' from ..."
    m = re.search(r"copying path '/nix/store/[a-z0-9]+-(.+?)'", line)
    if m:
        return f"Fetching {m.group(1)}"
    # "building '/nix/store/hash-name.drv'..."
    m = re.search(r"building '/nix/store/[a-z0-9]+-(.+?)\.drv'", line)
    if m:
        return f"Building {m.group(1)}"
    # "evaluating derivation ..." or just "evaluating"
    if "evaluating" in line.lower():
        return "Evaluating flake..."
    # Progress counters like "[3/5 built, 2 copied (10.2 MiB)]"
    m = re.match(r"\[[\d/]+ (?:built|copied|fetched).*\]", line.strip())
    if m:
        return line.strip()
    return ""


def _estimate_image_size(store_path: str, sentinel: Path) -> int:
    """Estimate the image stream size in bytes. Returns 0 if unknown."""
    # First, check if we saved a size from a previous stream
    size_file = sentinel.parent / f"{sentinel.name}-size"
    if size_file.exists():
        try:
            return int(size_file.read_text().strip())
        except (ValueError, OSError):
            pass
    # Fall back to nix closure size (approximates uncompressed image)
    try:
        r = subprocess.run(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command flakes",
                "path-info",
                "--closure-size",
                store_path,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            # Output format: "/nix/store/...\t<size>" or just the path with -S flag
            parts = r.stdout.strip().split()
            for p in reversed(parts):
                if p.isdigit():
                    return int(p)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return 0


def _build_image_store_path(
    repo_root: Path,
    extra_packages: Optional[List[Union[str, dict]]] = None,
    *,
    out_link: Path,
    status_message: str,
) -> tuple[Optional[str], list[str]]:
    """Run the nix image build and return the resulting store path on success."""
    build_env = os.environ.copy()
    pkg_json = json.dumps(extra_packages) if extra_packages else ""
    if extra_packages:
        build_env["YOLO_EXTRA_PACKAGES"] = pkg_json

    build_stderr_tail: list[str] = []
    try:
        process = subprocess.Popen(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                ".#dockerImage",
                "--impure",
                "--out-link",
                str(out_link),
                "--print-build-logs",
            ],
            cwd=repo_root,
            env=build_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return None, ["nix command not found"]

    with console.status(status_message, spinner="dots") as status:
        if process.stderr:
            for line in iter(process.stderr.readline, ""):
                clean = line.rstrip()
                if clean:
                    build_stderr_tail.append(clean)
                    if len(build_stderr_tail) > 30:
                        build_stderr_tail.pop(0)
                    summary = _summarize_nix_line(clean)
                    if summary:
                        status.update(f"[bold blue]{summary}[/bold blue]")

    process.wait()
    if process.returncode != 0:
        return None, build_stderr_tail

    return str(out_link.resolve()), build_stderr_tail


def _format_progress(current: int, estimate: int) -> str:
    """Format byte progress with optional percentage."""
    mb = current / (1024 * 1024)
    cur_str = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
    if estimate > 0:
        pct = min(int(current * 100 / estimate), 99)  # Cap at 99% until done
        return f"{cur_str} ({pct}%)"
    return cur_str


def _read_loaded_paths(sentinel: Path) -> set[str]:
    """Read the set of store paths that have been loaded into this runtime."""
    if not sentinel.exists():
        return set()
    return {line.strip() for line in sentinel.read_text().splitlines() if line.strip()}


def _add_loaded_path(sentinel: Path, store_path: str):
    """Add a store path to the sentinel, capping at 10 entries (LRU)."""
    paths = (
        [line.strip() for line in sentinel.read_text().splitlines() if line.strip()]
        if sentinel.exists()
        else []
    )
    # Remove if already present (will re-add at end as most recent)
    paths = [p for p in paths if p != store_path]
    paths.append(store_path)
    # Keep only the 10 most recent
    if len(paths) > 10:
        paths = paths[-10:]
    sentinel.write_text("\n".join(paths) + "\n")


def auto_load_image(
    repo_root: Path,
    extra_packages: List[Union[str, dict]] = None,
    runtime: str = "docker",
):
    """Cheaply check if the nix image needs to be reloaded into the container runtime."""
    # Per-runtime sentinel tracks all store paths loaded into this runtime
    sentinel = BUILD_DIR / f"last-load-{runtime}"
    out_link = BUILD_DIR / "run-result"
    pkg_json = json.dumps(extra_packages) if extra_packages else ""
    current_path, build_stderr_tail = _build_image_store_path(
        repo_root,
        extra_packages=extra_packages,
        out_link=out_link,
        status_message="[bold blue]Checking jail image...",
    )

    if current_path is None:
        err_summary = (
            "\n".join(build_stderr_tail[-10:]) if build_stderr_tail else "unknown error"
        )
        console.print(
            f"[yellow]Warning: nix build failed:[/yellow]\n[dim]{err_summary}[/dim]"
        )
        # If the image already exists in the runtime (e.g. pre-loaded inside a jail),
        # we can still proceed — just skip the load step.
        check = subprocess.run(
            [runtime, "image", "inspect", JAIL_IMAGE],
            capture_output=True,
        )
        if check.returncode == 0:
            console.print(f"[yellow]Using existing {JAIL_IMAGE} image.[/yellow]")
            return
        console.print(
            f"[bold red]No existing {JAIL_IMAGE} image found. Cannot start jail.[/bold red]"
        )
        return

    # 2. Check if this store path has already been loaded into the runtime
    loaded_paths = _read_loaded_paths(sentinel)

    if current_path not in loaded_paths:
        # Print the reason for the reload
        if not loaded_paths:
            console.print(
                f"[bold blue]Image load needed:[/bold blue] first run (no images loaded into {runtime} yet)"
            )
        else:
            console.print(
                "[bold blue]Image load needed:[/bold blue] nix store path changed"
            )
            console.print(f"  [dim]new: {current_path}[/dim]")
            if pkg_json:
                console.print(f"  [dim]packages: {pkg_json}[/dim]")
        try:
            with console.status(
                f"[bold cyan]Preparing image for {runtime}...", spinner="bouncingBar"
            ) as status:
                # Estimate size (uses saved size from last stream, or nix closure size)
                estimated_size = _estimate_image_size(current_path, sentinel)

                # streamLayeredImage produces a script that outputs the image tar to stdout.
                # Pipe it directly to `runtime load` — generation and loading run in parallel.
                status.update(f"[bold cyan]Starting image stream to {runtime}...")
                stream_proc = subprocess.Popen(
                    [current_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,  # Suppress "Creating layer N..." noise
                )
                load_proc = subprocess.Popen(
                    [runtime, "load"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                total_bytes = 0
                chunk_size = 1024 * 1024  # 1 MB
                while True:
                    chunk = stream_proc.stdout.read(chunk_size)
                    if not chunk:
                        break
                    load_proc.stdin.write(chunk)
                    total_bytes += len(chunk)
                    progress = _format_progress(total_bytes, estimated_size)
                    status.update(f"[bold cyan]Streaming to {runtime}... {progress}")
                load_proc.stdin.close()

            stream_proc.wait()
            load_proc.wait()

            if stream_proc.returncode != 0 or load_proc.returncode != 0:
                console.print(
                    f"[bold red]Error loading image into {runtime}.[/bold red]"
                )
            else:
                mb = total_bytes / (1024 * 1024)
                size_str = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
                console.print(
                    f"[bold green]Done: loaded image ({size_str})[/bold green]"
                )
                _add_loaded_path(sentinel, current_path)
                # Save actual size for accurate future estimates
                size_file = sentinel.parent / f"{sentinel.name}-size"
                size_file.write_text(str(total_bytes))
        except Exception as e:
            console.print(f"[bold red]Error streaming image: {e}[/bold red]")

    # Cleanup temp link
    out_link.unlink(missing_ok=True)


def _load_jsonc_file(path: Path, label: str, *, strict: bool = False) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            parsed = pyjson5.load(f)
        if isinstance(parsed, dict):
            return parsed
        msg = f"{label} must contain a top-level JSON object"
        if strict:
            raise ConfigError(msg)
        typer.echo(f"Warning: {msg}", err=True)
        return {}
    except Exception as e:
        if strict:
            raise ConfigError(f"Failed to parse {label}: {e}") from e
        typer.echo(f"Warning: Failed to parse {label}: {e}", err=True)
        return {}


def _merge_lists(base: List[Any], override: List[Any]) -> List[Any]:
    merged = list(base)
    seen = {json.dumps(item, sort_keys=True, default=str) for item in merged}
    for item in override:
        key = json.dumps(item, sort_keys=True, default=str)
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_config(result[key], value)
        elif (
            key in result and isinstance(result[key], list) and isinstance(value, list)
        ):
            result[key] = _merge_lists(result[key], value)
        else:
            result[key] = value
    return result


def load_config(
    workspace: Optional[Path] = None, *, strict: bool = False
) -> Dict[str, Any]:
    workspace = workspace or Path.cwd()
    user_config = _load_jsonc_file(
        USER_CONFIG_PATH, str(USER_CONFIG_PATH), strict=strict
    )
    workspace_config = _load_jsonc_file(
        workspace / "yolo-jail.jsonc", "yolo-jail.jsonc", strict=strict
    )
    return merge_config(user_config, workspace_config)


KNOWN_TOP_LEVEL_CONFIG_KEYS = {
    "runtime",
    "repo_path",
    "packages",
    "mounts",
    "network",
    "security",
    "mise_tools",
    "lsp_servers",
    "mcp_servers",
    "devices",
}
KNOWN_NETWORK_KEYS = {"mode", "ports", "forward_host_ports"}
KNOWN_SECURITY_KEYS = {"blocked_tools"}
KNOWN_BLOCKED_TOOL_KEYS = {"name", "message", "suggestion"}
KNOWN_PACKAGE_KEYS = {"name", "nixpkgs", "version", "url", "hash"}
KNOWN_LSP_SERVER_KEYS = {"command", "args", "fileExtensions"}
KNOWN_MCP_SERVER_KEYS = {"command", "args"}
KNOWN_DEVICE_KEYS = {"usb", "description", "cgroup_rule"}
USB_ID_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$")


def _report_unknown_keys(
    mapping: Dict[str, Any], allowed: set[str], path: str, errors: List[str]
):
    for key in sorted(mapping):
        if key not in allowed:
            errors.append(f"{path}.{key}: unknown key")


def _validate_string_list(values: Any, path: str, errors: List[str]):
    if not isinstance(values, list):
        errors.append(f"{path}: expected a list")
        return
    for idx, value in enumerate(values):
        if not isinstance(value, str):
            errors.append(f"{path}[{idx}]: expected a string")


def _validate_port_number(value: Any, path: str, errors: List[str]):
    try:
        port = int(value)
    except (TypeError, ValueError):
        errors.append(f"{path}: expected an integer port number")
        return
    if port < 1 or port > 65535:
        errors.append(f"{path}: port must be between 1 and 65535")


def _validate_publish_port(value: Any, path: str, errors: List[str]):
    if not isinstance(value, str):
        errors.append(f"{path}: expected a string like '8000:8000'")
        return
    base = value
    if "/" in base:
        base, protocol = base.rsplit("/", 1)
        if protocol not in ("tcp", "udp"):
            errors.append(f"{path}: protocol must be tcp or udp")
    parts = base.split(":")
    if len(parts) == 2:
        host_port, container_port = parts
    elif len(parts) == 3:
        _, host_port, container_port = parts
    else:
        errors.append(f"{path}: expected 'host:container' or 'ip:host:container'")
        return
    _validate_port_number(host_port, f"{path}.host", errors)
    _validate_port_number(container_port, f"{path}.container", errors)


def _validate_forward_host_port(value: Any, path: str, errors: List[str]):
    if isinstance(value, int):
        _validate_port_number(value, path, errors)
        return
    if not isinstance(value, str):
        errors.append(f"{path}: expected an int or string like '8080:9090'")
        return
    parts = value.split(":")
    if len(parts) == 1:
        _validate_port_number(parts[0], path, errors)
        return
    if len(parts) == 2:
        _validate_port_number(parts[0], f"{path}.local", errors)
        _validate_port_number(parts[1], f"{path}.host", errors)
        return
    errors.append(f"{path}: expected '<port>' or '<local>:<host>'")


def _validate_config(
    config: Dict[str, Any], workspace: Optional[Path] = None
) -> tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    workspace = workspace or Path.cwd()

    _report_unknown_keys(config, KNOWN_TOP_LEVEL_CONFIG_KEYS, "config", errors)

    runtime = config.get("runtime")
    if runtime is not None and runtime not in ("podman", "docker"):
        errors.append("config.runtime: expected 'podman' or 'docker'")

    repo_path = config.get("repo_path")
    if repo_path is not None and not isinstance(repo_path, str):
        errors.append("config.repo_path: expected a string path")

    packages = config.get("packages")
    if packages is not None:
        if not isinstance(packages, list):
            errors.append("config.packages: expected a list")
        else:
            for idx, pkg in enumerate(packages):
                path = f"config.packages[{idx}]"
                if isinstance(pkg, str):
                    continue
                if not isinstance(pkg, dict):
                    errors.append(f"{path}: expected a string or object")
                    continue
                _report_unknown_keys(pkg, KNOWN_PACKAGE_KEYS, path, errors)
                if not isinstance(pkg.get("name"), str):
                    errors.append(f"{path}.name: expected a string")
                has_nixpkgs = "nixpkgs" in pkg
                has_version_override = any(
                    key in pkg for key in ("version", "url", "hash")
                )
                if has_nixpkgs:
                    if not isinstance(pkg.get("nixpkgs"), str):
                        errors.append(f"{path}.nixpkgs: expected a string")
                    if has_version_override:
                        errors.append(
                            f"{path}: use either nixpkgs pinning or version/url/hash overrides, not both"
                        )
                elif has_version_override:
                    for key in ("version", "url", "hash"):
                        if not isinstance(pkg.get(key), str):
                            errors.append(f"{path}.{key}: expected a string")
                else:
                    errors.append(
                        f"{path}: object packages must use either 'nixpkgs' or 'version'+'url'+'hash'"
                    )

    mounts = config.get("mounts")
    if mounts is not None:
        if not isinstance(mounts, list):
            errors.append("config.mounts: expected a list")
        else:
            for idx, mount in enumerate(mounts):
                path = f"config.mounts[{idx}]"
                if not isinstance(mount, str):
                    errors.append(f"{path}: expected a string")
                    continue
                colon_idx = mount.rfind(":")
                host_path = mount
                if colon_idx > 0 and mount[colon_idx + 1 : colon_idx + 2] == "/":
                    host_path = mount[:colon_idx]
                    container_path = mount[colon_idx + 1 :]
                    if not container_path.startswith("/"):
                        errors.append(f"{path}: container mount path must be absolute")
                if not host_path:
                    errors.append(f"{path}: host mount path cannot be empty")
                    continue
                resolved_host = Path(host_path).expanduser().resolve()
                if not resolved_host.exists():
                    warnings.append(
                        f"{path}: host path does not exist and will be skipped: {resolved_host}"
                    )

    network = config.get("network")
    if network is not None:
        if not isinstance(network, dict):
            errors.append("config.network: expected an object")
        else:
            _report_unknown_keys(network, KNOWN_NETWORK_KEYS, "config.network", errors)
            mode = network.get("mode")
            if mode is not None and mode not in ("bridge", "host"):
                errors.append("config.network.mode: expected 'bridge' or 'host'")
            ports = network.get("ports")
            if ports is not None:
                if not isinstance(ports, list):
                    errors.append("config.network.ports: expected a list")
                else:
                    for idx, port in enumerate(ports):
                        _validate_publish_port(
                            port, f"config.network.ports[{idx}]", errors
                        )
            forward_host_ports = network.get("forward_host_ports")
            if forward_host_ports is not None:
                if not isinstance(forward_host_ports, list):
                    errors.append("config.network.forward_host_ports: expected a list")
                else:
                    for idx, port in enumerate(forward_host_ports):
                        _validate_forward_host_port(
                            port,
                            f"config.network.forward_host_ports[{idx}]",
                            errors,
                        )
            if mode == "host":
                if network.get("ports"):
                    warnings.append(
                        "config.network.ports: ignored when network.mode is 'host'"
                    )
                if network.get("forward_host_ports"):
                    warnings.append(
                        "config.network.forward_host_ports: ignored when network.mode is 'host'"
                    )

    security = config.get("security")
    if security is not None:
        if not isinstance(security, dict):
            errors.append("config.security: expected an object")
        else:
            _report_unknown_keys(
                security, KNOWN_SECURITY_KEYS, "config.security", errors
            )
            blocked_tools = security.get("blocked_tools")
            if blocked_tools is not None:
                if not isinstance(blocked_tools, list):
                    errors.append("config.security.blocked_tools: expected a list")
                else:
                    for idx, tool in enumerate(blocked_tools):
                        path = f"config.security.blocked_tools[{idx}]"
                        if isinstance(tool, str):
                            continue
                        if not isinstance(tool, dict):
                            errors.append(f"{path}: expected a string or object")
                            continue
                        _report_unknown_keys(
                            tool, KNOWN_BLOCKED_TOOL_KEYS, path, errors
                        )
                        if not isinstance(tool.get("name"), str):
                            errors.append(f"{path}.name: expected a string")
                        for key in ("message", "suggestion"):
                            if key in tool and not isinstance(tool.get(key), str):
                                errors.append(f"{path}.{key}: expected a string")

    mise_tools = config.get("mise_tools")
    if mise_tools is not None:
        if not isinstance(mise_tools, dict):
            errors.append("config.mise_tools: expected an object")
        else:
            for key, value in mise_tools.items():
                if not isinstance(key, str):
                    errors.append("config.mise_tools: tool names must be strings")
                if not isinstance(value, str):
                    errors.append(f"config.mise_tools.{key}: expected a version string")

    lsp_servers = config.get("lsp_servers")
    if lsp_servers is not None:
        if not isinstance(lsp_servers, dict):
            errors.append("config.lsp_servers: expected an object")
        else:
            for name, cfg in lsp_servers.items():
                path = f"config.lsp_servers.{name}"
                if not isinstance(cfg, dict):
                    errors.append(f"{path}: expected an object")
                    continue
                _report_unknown_keys(cfg, KNOWN_LSP_SERVER_KEYS, path, errors)
                if not isinstance(cfg.get("command"), str):
                    errors.append(f"{path}.command: expected a string")
                if "args" in cfg:
                    _validate_string_list(cfg["args"], f"{path}.args", errors)
                file_extensions = cfg.get("fileExtensions")
                if not isinstance(file_extensions, dict):
                    errors.append(f"{path}.fileExtensions: expected an object")
                else:
                    for ext, lang in file_extensions.items():
                        if not isinstance(ext, str) or not isinstance(lang, str):
                            errors.append(
                                f"{path}.fileExtensions: keys and values must be strings"
                            )

    mcp_servers = config.get("mcp_servers")
    if mcp_servers is not None:
        if not isinstance(mcp_servers, dict):
            errors.append("config.mcp_servers: expected an object")
        else:
            for name, cfg in mcp_servers.items():
                path = f"config.mcp_servers.{name}"
                if cfg is None:
                    continue
                if not isinstance(cfg, dict):
                    errors.append(f"{path}: expected an object or null")
                    continue
                _report_unknown_keys(cfg, KNOWN_MCP_SERVER_KEYS, path, errors)
                if not isinstance(cfg.get("command"), str):
                    errors.append(f"{path}.command: expected a string")
                if "args" in cfg:
                    _validate_string_list(cfg["args"], f"{path}.args", errors)

    devices = config.get("devices")
    if devices is not None:
        if not isinstance(devices, list):
            errors.append("config.devices: expected a list")
        else:
            for idx, device in enumerate(devices):
                path = f"config.devices[{idx}]"
                if isinstance(device, str):
                    if not Path(device).exists():
                        warnings.append(
                            f"{path}: device path does not exist and may be skipped: {device}"
                        )
                    continue
                if not isinstance(device, dict):
                    errors.append(f"{path}: expected a string or object")
                    continue
                _report_unknown_keys(device, KNOWN_DEVICE_KEYS, path, errors)
                has_usb = "usb" in device
                has_cgroup = "cgroup_rule" in device
                if has_usb == has_cgroup:
                    errors.append(
                        f"{path}: expected exactly one of 'usb' or 'cgroup_rule'"
                    )
                    continue
                if has_usb:
                    if not isinstance(device.get("usb"), str):
                        errors.append(f"{path}.usb: expected a string")
                    elif not USB_ID_RE.match(device["usb"]):
                        errors.append(
                            f"{path}.usb: expected vendor:product hex format like '0bda:2838'"
                        )
                    if "description" in device and not isinstance(
                        device.get("description"), str
                    ):
                        errors.append(f"{path}.description: expected a string")
                if has_cgroup and not isinstance(device.get("cgroup_rule"), str):
                    errors.append(f"{path}.cgroup_rule: expected a string")

    return errors, warnings


def _runtime_for_check(config: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the effective runtime without exiting."""
    env = os.environ.get("YOLO_RUNTIME")
    if env and env in ("podman", "docker"):
        if shutil.which(env):
            return env, None
        return None, f"Configured runtime '{env}' from YOLO_RUNTIME is not on PATH"

    cfg = config.get("runtime")
    if cfg and cfg in ("podman", "docker"):
        if shutil.which(cfg):
            return cfg, None
        return None, f"Configured runtime '{cfg}' from yolo-jail.jsonc is not on PATH"

    for rt in ("podman", "docker"):
        if shutil.which(rt):
            return rt, None
    return None, "No container runtime found on PATH"


def _entrypoint_preflight(repo_root: Path, workspace: Path, config: Dict[str, Any]):
    """Generate jail-managed config into a temp home to catch config/render errors."""
    src_dir = repo_root / "src"
    host_mise = _host_mise_dir()
    normalized_blocked = _normalize_blocked_tools(config.get("security"))
    env = os.environ.copy()

    with tempfile.TemporaryDirectory(prefix="yolo-check-") as tmp:
        env.update(
            {
                "JAIL_HOME": tmp,
                "HOME": tmp,
                "NPM_CONFIG_PREFIX": f"{tmp}/.npm-global",
                "GOPATH": f"{tmp}/go",
                "MISE_DATA_DIR": str(host_mise),
                "YOLO_HOST_DIR": str(workspace.resolve()),
                "YOLO_BLOCK_CONFIG": json.dumps(normalized_blocked),
                "YOLO_MISE_TOOLS": json.dumps(_merge_mise_tools(config)),
                "YOLO_LSP_SERVERS": json.dumps(config.get("lsp_servers", {})),
                "YOLO_MCP_SERVERS": json.dumps(config.get("mcp_servers", {})),
            }
        )
        code = f"""
import json
import sys
from pathlib import Path

sys.path.insert(0, {str(src_dir)!r})
import entrypoint

entrypoint.generate_shims()
entrypoint.generate_bashrc()
entrypoint.generate_bootstrap_script()
entrypoint.generate_venv_precreate_script()
entrypoint.generate_mise_config()
entrypoint.generate_mcp_wrappers()
entrypoint.configure_copilot()
entrypoint.configure_gemini()

json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
json.loads((entrypoint.COPILOT_DIR / "lsp-config.json").read_text())
json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
print("ok")
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            details = "\n".join(
                part for part in (result.stdout.strip(), result.stderr.strip()) if part
            )
            raise ConfigError(details or "entrypoint dry-run failed")


@app.command()
def init():
    """Initialize a yolo-jail.jsonc config and print an agent briefing."""
    config_path = Path.cwd() / "yolo-jail.jsonc"
    if config_path.exists():
        typer.echo("yolo-jail.jsonc already exists.")
        _print_init_briefing(config_path)
        return

    content = """{
  // Container runtime: "podman" or "docker" (also settable via YOLO_RUNTIME env var)
  // "runtime": "podman",

  // Extra nix packages to include in the jail image.
  // Names must match nixpkgs attribute names (search at https://search.nixos.org/packages).
  // The image rebuilds only when this list changes.
  // Supports plain strings (latest), pinned nixpkgs commits, or version overrides:
  // "packages": [
  //   "postgresql",
  //   {"name": "freetype", "nixpkgs": "<commit-hash>"},
  //   {"name": "freetype", "version": "2.14.1",
  //    "url": "mirror://savannah/freetype/freetype-2.14.1.tar.xz",
  //    "hash": "sha256-..."}
  // ],
  // Find nixpkgs commits for specific versions at: https://lazamar.co.uk/nix-versions/

  "security": {
    // Tools to block. Can be a simple string or an object with custom messages.
    "blocked_tools": [
      {
        "name": "grep",
        "message": "Use 'rg' (ripgrep) for faster searching.",
        "suggestion": "rg <pattern>"
      },
      {
        "name": "find",
        "message": "Use 'fd' for faster file finding."
      }
    ]
  },
  "network": {
    // "bridge" (default) or "host"
    "mode": "bridge",
    // Ports to publish in bridge mode ["Host:Container"]
    // "ports": ["8000:8000"]
    // Forward host ports into the jail (appear on localhost inside container)
    // "forward_host_ports": [5432, "8080:9090"]
  },
  // Extra host paths to mount read-only into the jail for context.
  // Each entry is a host path (mounted at /ctx/<basename>) or "host:container".
  // "mounts": [
  //   "~/code/other-repo",
  //   "~/code/shared-lib:/ctx/shared-lib"
  // ]

  // Extra tools to install via mise (key: tool name, value: version string).
  // Default: {"neovim": "stable"} — override in user or workspace config.
  // "mise_tools": {"neovim": "nightly", "typst": "latest"}

  // Additional language servers for Copilot and Gemini.
  // Defaults (always present): python (pyright), typescript, go (gopls).
  // Add new servers or override defaults. Binary must be on PATH (e.g., via mise_tools).
  // "lsp_servers": {
  //   "rust": {
  //     "command": "rust-analyzer",
  //     "args": [],
  //     "fileExtensions": {".rs": "rust"}
  //   }
  // }
  //
  // Additional MCP servers for Copilot and Gemini.
  // Defaults: chrome-devtools, sequential-thinking.
  // Add new servers, override defaults, or set a server to null to disable it.
  // Binary must be on PATH or absolute.
  // "mcp_servers": {
  //   "my-custom": {
  //     "command": "/workspace/scripts/my-mcp-server.py",
  //     "args": []
  //   },
  //   "chrome-devtools": null
  // }
}
"""
    with open(config_path, "w") as f:
        f.write(content)
    typer.echo("Created yolo-jail.jsonc")

    # Add .yolo/ to .gitignore if not already present
    gitignore = Path.cwd() / ".gitignore"
    if gitignore.exists():
        text = gitignore.read_text()
        if ".yolo/" not in text:
            with open(gitignore, "a") as f:
                f.write("\n# YOLO Jail workspace state\n.yolo/\n")
    else:
        with open(gitignore, "w") as f:
            f.write("# YOLO Jail workspace state\n.yolo/\n")

    _print_init_briefing(config_path)


def _print_init_briefing(config_path: Path):
    """Print the comprehensive agent briefing after init."""
    console.print(f"""
[bold green]✓ Config ready:[/bold green] {config_path}

[bold]═══════════════════════════════════════════════════════════[/bold]
[bold]  YOLO JAIL — AGENT BRIEFING                              [/bold]
[bold]═══════════════════════════════════════════════════════════[/bold]

You are about to be launched inside a YOLO Jail — an isolated container
that protects the host system from unintended changes. Here is what you
need to know.

[bold cyan]WHAT YOU KEEP (shared with the host)[/bold cyan]

  • [bold]Your workspace[/bold] is bind-mounted at [bold]/workspace[/bold] (read-write).
    Every file edit you make is visible on the host immediately.
    This is the SAME directory, not a copy.

  • [bold]Internet access[/bold] is available (bridge networking by default).
    You can curl, pip install, npm install, go get, etc.

  • [bold]Git/jj identity[/bold] is injected from the host (name + email).
    GitHub CLI (gh) is pre-authenticated.

  • [bold]Mise tool data[/bold] is shared with the host. Python, Node, Go, and
    other runtimes installed via mise are available at the same paths.
    Venvs created on the host resolve inside the jail.

[bold cyan]WHAT CHANGES (different from the host)[/bold cyan]

  • [bold]Home directory[/bold] is /home/agent. This is a [bold]shared home[/bold] — the same
    across ALL jail workspaces. Auth tokens, tool caches, shell configs,
    and installed tools all persist here across restarts. It is separate
    from the host home directory.

  • [bold]Per-workspace state[/bold]: Some things are isolated per-workspace
    (not shared across jails): SSH keys, bash history, copilot sessions,
    gemini history. These live in <workspace>/.yolo/.

  • [bold]Workspace path[/bold] is /workspace (not the host's absolute path).
    Venv scripts with absolute host path shebangs may need fixing.

  • [bold]Some tools are blocked[/bold] (e.g., grep → rg, find → fd).
    Set YOLO_BYPASS_SHIMS=1 in scripts that need the originals.

[bold cyan]TOOLS AVAILABLE INSIDE[/bold cyan]

  Runtimes:  Node.js 22, Python 3.13, Go (managed by mise)
  Editors:   nvim (stable by default, configurable via mise_tools)
  CLI tools: rg, fd, bat, jq, git, jj, gh, curl, strace, uv, tmux
  Agents:    copilot, gemini (auto-injected with --yolo flag)
  The 'yolo' command itself is available inside for nested jailing.

  [bold]Mise[/bold] manages all runtimes and supports thousands of tools from
  multiple registries (aqua, asdf, cargo, go, npm, pipx, ubi, and more).
  Run 'mise registry' inside the jail to browse. Add tools to the
  "mise_tools" config or to /workspace/mise.toml for the workspace.
  Examples: rust, zig, terraform, kubectl, typst, pixi, conda.

[bold cyan]WHAT TO DO NOW — TRANSITION QUICKLY[/bold cyan]

  [bold]Your goal is to get inside the jail as fast as possible.[/bold]
  Do only what's needed outside, then hand off. All real work happens
  inside the jail where you have full tool access.

  1. [bold]Review yolo-jail.jsonc[/bold] — edit it [bold]only[/bold] if you need extra packages.
     • "packages": nix packages baked into the image (rebuilds on change).
       Search: https://search.nixos.org/packages
     • "mise_tools": tools installed via mise (no rebuild needed).
       For tools with binary releases — fast, no compilation.
     Most tasks need NO config changes. Skip this step if unsure.

  2. [bold]Run `yolo check`[/bold] after [bold]EVERY[/bold] `yolo-jail.jsonc` edit to validate
     the config and preflight the build. Use `yolo check --no-build` inside a
     running jail if you only need config/entrypoint validation. Do this before
     asking the human to restart you into the jail.

  3. [bold](MANDATORY) Write a handover document[/bold] at:
     [bold yellow].yolo/handover.md[/bold yellow]

     This file is [bold]required[/bold]. Your jail instance will be a completely
     fresh agent session with NO access to this conversation. Without
     this document, the inner agent starts blind. Include:
     • What you were working on and the current state
     • What remains to be done (specific tasks, not vague goals)
     • Key decisions made and why
     • Files to look at first
     • Any gotchas or context the inner agent needs

  4. [bold]Ask the human to restart you inside the jail[/bold]:
     Tell them to run: yolo -- copilot  (or yolo -- gemini)

     The inner agent has a built-in [bold]jail-startup[/bold] skill that reads
     your handover doc automatically. The human just needs to say:
     [bold yellow]"invoke the jail-startup skill"[/bold yellow]
     and the inner agent will pick up your handover and continue.

  Do NOT spend time on implementation outside the jail. Write the
  handover doc, request the restart, and stop. The inner agent has
  the same tools and full internet access — it can do everything.

[bold cyan]CONFIGURATION REFERENCE[/bold cyan]

  Run 'yolo config-ref' for the full field reference.
  Run 'yolo --help' for usage examples.
""")


@app.command("init-user-config")
def init_user_config():
    """Initialize a user-level config at ~/.config/yolo-jail/config.jsonc."""
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if USER_CONFIG_PATH.exists():
        typer.echo(f"{USER_CONFIG_PATH} already exists.")
        return
    content = """{
  // User-level defaults merged into every project config.
  // Lists are merged (deduplicated), scalars are overridden by workspace config.
  // Container runtime: "podman" or "docker" (also settable via YOLO_RUNTIME env var)
  // "runtime": "podman",
  // "packages": ["sqlite", "postgresql"],
  // "mounts": ["~/code/shared-lib:/ctx/shared-lib"],
  // "security": {
  //   "blocked_tools": ["wget"]
  // }
}
"""
    with open(USER_CONFIG_PATH, "w") as f:
        f.write(content)
    typer.echo(f"Created {USER_CONFIG_PATH}")


@app.command("config-ref")
def config_ref():
    """Show the full YOLO Jail configuration reference."""
    console.print("""[bold]YOLO Jail Configuration Reference[/bold]

[bold cyan]CONFIG FILE: yolo-jail.jsonc[/bold cyan]

  Location: Project root (per-workspace)
  Format:   JSON with comments (JSONC)
  User defaults: ~/.config/yolo-jail/config.jsonc

  Workspace config merges over user defaults.
  Lists are merged and deduplicated. Scalars override.

  [bold yellow]Rule:[/bold yellow] After [bold]EVERY[/bold] edit to `yolo-jail.jsonc` or
  `~/.config/yolo-jail/config.jsonc`, run `yolo check` before restarting or
  asking a human to restart the jail. Use `yolo check --no-build` inside a
  running jail for a faster preflight.

[bold cyan]FIELDS[/bold cyan]

  [bold]runtime[/bold] (string): Container runtime.
    Values: "podman" (preferred) or "docker"
    Override: YOLO_RUNTIME env var takes priority.
    Auto-detect: prefers podman, falls back to docker.

  [bold]packages[/bold] (array): Extra nix packages baked into the image.
    Supports three formats:
    • String: package name from nixpkgs (latest from flake's pin)
      Example: "postgresql"
    • Object with nixpkgs: pinned to a specific nixpkgs commit
      Example: {"name": "freetype", "nixpkgs": "<commit-hash>"}
    • Object with version override: build from upstream source
      Uses the existing nix build recipe but swaps version+source.
      Example: {"name": "freetype", "version": "2.14.1",
                "url": "mirror://savannah/freetype/freetype-2.14.1.tar.xz",
                "hash": "sha256-MkJ+jEcawJWFMhKjeu+BbGC0IFLU2eSCMLqzvfKTbMw="}
      Get the hash: nix-prefetch-url <url>  (then convert with nix hash)
      Or set hash to "" and nix will tell you the correct one on build failure.
    Find nixpkgs commits per version: https://lazamar.co.uk/nix-versions/
    Search package names: https://search.nixos.org/packages
    Image rebuilds only when this list changes.
    Nix caches builds — identical configs across jails share cached results.

  [bold]mounts[/bold] (array of strings): Extra host paths mounted read-only.
    Simple path → mounted at /ctx/<basename>
    "host:container" → custom container path
    Example: ["/path/to/repo", "~/lib:/ctx/lib"]

  [bold]network.mode[/bold] (string): Network isolation mode.
    "bridge" (default): Isolated. Use network.ports for access.
    "host": Share host network stack (localhost works directly).

  [bold]network.ports[/bold] (array of strings): Port mappings in bridge mode.
    Format: "host_port:container_port"
    Example: ["8000:8000", "3000:3000"]
    Makes container services reachable from the host.

  [bold]network.forward_host_ports[/bold] (array): Forward host ports into the jail.
    Makes host services appear on localhost inside the container, even if the
    host service only listens on 127.0.0.1 (like SSH -L port forwarding).
    Integer: same port on both sides (e.g., 5432)
    String "local:host": remap ports (e.g., "5432:3306")
    Example: [5432, 6379, "8080:9090"]
    Uses socat via Unix sockets; only active in bridge mode.
    Requires socat installed on the host.

  [bold]security.blocked_tools[/bold] (array): Tools to block inside the jail.
    Simple: ["curl", "wget"]
    Detailed: [{"name": "grep", "message": "Use rg", "suggestion": "rg <pattern>"}]
    Default: grep and find are blocked (rg/fd suggested instead).
    Bypass: Set YOLO_BYPASS_SHIMS=1 in scripts that need blocked tools.

  [bold]mise_tools[/bold] (object): Extra tools installed via mise in the jail.
    Keys are mise tool names, values are version strings.
    Default: {"neovim": "stable"}
    These are injected into the jail's global mise config (not workspace mise.toml).
    Deep-merged: user config adds tools, workspace config overrides versions.
    Example: {"neovim": "nightly", "typst": "latest"}

  [bold]lsp_servers[/bold] (object): Additional language servers for Copilot and Gemini.
    Default servers (always present): python (pyright), typescript, go (gopls).
    Workspace servers are merged with defaults — add new ones or override existing.
    Each key is a server name; value is an object with:
      • command (string, required): Binary name (on PATH) or absolute path.
      • args (array of strings): Args passed to the LSP binary. Default: [].
      • fileExtensions (object): Extension → language ID map (required for Copilot).
    The entrypoint translates these for each agent:
      • Copilot: written to ~/.copilot/lsp-config.json as native LSP servers.
      • Gemini: wrapped via mcp-language-server as MCP servers in settings.json.
    Example: {"rust": {"command": "rust-analyzer", "args": [],
              "fileExtensions": {".rs": "rust"}}}

  [bold]mcp_servers[/bold] (object): MCP servers for Copilot and Gemini.
    Default servers: chrome-devtools, sequential-thinking.
    Workspace servers are merged with defaults — add new ones, override existing,
    or set a server to [bold]null[/bold] to disable a default or inherited server.
    Each key is a server name; value is an object with:
      • command (string, required): Binary name (on PATH) or absolute path.
      • args (array of strings): Args passed to the MCP server. Default: [].
    The entrypoint translates these for each agent:
      • Copilot: written to ~/.copilot/mcp-config.json.
      • Gemini: written to ~/.gemini/settings.json.
    Example: {"my-custom": {"command": "/workspace/scripts/my-mcp.py", "args": []},
              "chrome-devtools": null}

  [bold]devices[/bold] (array): Host devices to pass through to the jail.
    Three formats supported:
    • USB by vendor:product ID (preferred — stable across reboots):
      {"usb": "0bda:2838", "description": "RTL-SDR Blog V4"}
      Resolved to /dev/bus/usb/... at startup via lsusb.
    • Raw device path (fragile — changes on replug):
      "/dev/bus/usb/001/004"
    • Cgroup rule (broad access):
      {"cgroup_rule": "c 189:* rwm"}
      Grants access to all devices matching the major number.
    Missing devices produce a warning, not an error — the jail still starts.
    Subject to config change safety (human approval required).

[bold cyan]EXAMPLE CONFIG[/bold cyan]

  {
    "runtime": "podman",
    "mise_tools": {"neovim": "nightly"},
    "lsp_servers": {
      "rust": {"command": "rust-analyzer", "args": [],
               "fileExtensions": {".rs": "rust"}}
    },
    "packages": [
      "strace",
      {"name": "freetype", "nixpkgs": "e6f23dc0..."},
      {"name": "freetype", "version": "2.14.1",
       "url": "mirror://savannah/freetype/freetype-2.14.1.tar.xz",
       "hash": "sha256-MkJ+jEcawJWFMhKjeu+BbGC0IFLU2eSCMLqzvfKTbMw="}
    ],
    "mounts": ["/path/to/ref-repo"],
    "devices": [
      {"usb": "0bda:2838", "description": "RTL-SDR Blog V4"}
    ],
    "network": {
      "mode": "bridge",
      "ports": ["8000:8000"],
      "forward_host_ports": [5432]
    },
    "security": {
      "blocked_tools": [
        {"name": "grep", "message": "Use rg", "suggestion": "rg <pattern>"},
        "wget"
      ]
    }
  }

[bold cyan]ENVIRONMENT VARIABLES[/bold cyan]

  YOLO_RUNTIME          Override container runtime (podman/docker)
  YOLO_BYPASS_SHIMS     Set to 1 to bypass blocked tool shims
  YOLO_EXTRA_PACKAGES   JSON array of extra nix packages (internal)

[bold cyan]CONFIG CHANGE SAFETY[/bold cyan]

  When yolo-jail.jsonc changes between jail startups, the CLI shows a
  diff of the normalized config and asks for y/N confirmation. This
  prevents agents from silently adding packages or mounts without the
  human operator noticing. Agents should still run `yolo check` after
  every config edit before asking for that restart.

  - First run: config is accepted and a snapshot saved.
  - Subsequent runs: changes require explicit y/N approval.
  - Non-interactive (piped input): accepted with a warning.

  Snapshot location: <workspace>/.yolo/config-snapshot.json

[bold cyan]AGENT PACKAGE WORKFLOW[/bold cyan]

  Agents inside the jail can request new packages:

  1. Agent edits /workspace/yolo-jail.jsonc, adds to "packages" array
  2. Agent ALWAYS runs `yolo check` after the edit (`--no-build` is okay inside a running jail)
  3. If the check passes, agent tells the human: "Please restart the jail for new packages"
  4. On next startup, human sees the config diff and approves (y/N)
  5. Image rebuilds with the new package
  6. Agent can use the package after restart

  This keeps the human in the loop for all environment changes.
  Do NOT install packages via apt, nix-env, or other package managers.

  [bold cyan]COMMANDS[/bold cyan]

  yolo                      Start interactive jail shell
  yolo -- <command>         Run a command inside the jail
  yolo --new -- <command>   Force a new container
  yolo check                Validate config and preflight the build
  yolo ps                   List running jail containers
  yolo init                 Create yolo-jail.jsonc in current directory
  yolo init-user-config     Create user-level defaults config
  yolo config-ref           Show this reference

[bold cyan]INSIDE THE JAIL[/bold cyan]

  [bold]Workspace[/bold]
    Your project is bind-mounted read-write at /workspace.
    Edits are visible on the host immediately — this is the SAME directory.
    The workspace path changes from the host path to /workspace.

  [bold]Networking[/bold]
    Full internet access is available. Bridge mode (default) isolates the
    container network but allows outbound connections. Use network.ports
    to publish container ports to the host. Host mode shares the host
    network stack directly.

  [bold]Home Directory (/home/agent)[/bold]
    A shared persistent home that is the SAME across ALL jail workspaces.
    Contains: auth tokens (gh, gemini), tool caches, npm/go globals,
    nvim config, shell configs, mise tool data. All of this survives
    jail restarts and is shared between every project's jail.

  [bold]Per-Workspace State[/bold]
    Some state is isolated per-workspace (in <workspace>/.yolo/):
    SSH keys, bash history, copilot sessions, gemini history.
    These are NOT shared across different project jails.

  [bold]Identity & Auth[/bold]
    Git/jj identity (name + email) is injected from the host automatically.
    GitHub CLI (gh) is pre-authenticated via the shared home.
    SSH keys are per-workspace — configure in <workspace>/.yolo/home/ssh/.

  [bold]Tools & Runtimes[/bold]
    Runtimes: Node.js 22, Python 3.13, Go (managed by mise)
    Editors:  nvim (version configurable via mise_tools config)
    CLI:      rg, fd, bat, jq, git, jj, gh, curl, strace, uv, tmux
    Agents:   copilot, gemini (--yolo auto-injected)
    The 'yolo' command is available inside for nested jailing and help.

  [bold]Mise Tool Management[/bold]
    Mise manages all runtimes and supports thousands of tools from
    multiple registries:
    • aqua — pre-built binaries (kubectl, terraform, gh, etc.)
    • asdf — version-managed runtimes (python, node, ruby, etc.)
    • cargo — Rust crates (ripgrep, fd-find, bat, etc.)
    • go — Go modules (built from source)
    • npm — Node packages (installed globally)
    • pipx — Python CLI tools (isolated envs)
    • ubi — universal binary installer (GitHub releases)
    Run 'mise registry' to browse all available tools. Add tools via:
    • "mise_tools" in yolo-jail.jsonc (injected into jail global config)
    • /workspace/mise.toml (workspace-specific, checked into git)
    The host's mise data directory is shared with the jail, so tool
    installs are available in both environments.

  [bold]Blocked Tools[/bold]
    By default, grep is replaced by rg and find by fd. These are shims —
    set YOLO_BYPASS_SHIMS=1 in scripts that need the real commands.
    Configure via security.blocked_tools in yolo-jail.jsonc.

  [bold]Venvs & Python[/bold]
    The host's mise data directory is shared with the jail, so venvs
    created on the host resolve inside the jail (python binary paths
    match). The workspace path changes to /workspace though, so
    venv scripts with absolute shebangs may need fixing.

  [bold]Persistence Summary[/bold]
    Shared home:   /home/agent (same across all jails — auth, tools, caches)
    Workspace:     /workspace edits visible on host immediately
    Per-workspace: SSH keys, bash history, copilot/gemini sessions
    Ephemeral:     /tmp, container processes

[bold cyan]SPAWNING A NEW PROJECT[/bold cyan]

  When setting up a new project for jail use:

  1. Run 'yolo init' in the project root to create yolo-jail.jsonc
  2. Edit the config — add any nix packages or mise_tools needed
  3. Run 'yolo check' after EVERY config edit to validate the config before restarting
  4. Run 'yolo -- bash' to enter the jail interactively
  5. Start your agent: 'yolo -- copilot' or 'yolo -- gemini'

  [bold]For agents preparing to enter a jail:[/bold]
  Before asking the human to restart you inside the jail, ALWAYS run 'yolo check'
  and write a
  handoff document (e.g., scratch/jail-notes.md) with:
  • Current task state and what remains to be done
  • Decisions made and their rationale
  • Key files to examine first
  Your inner-jail self will be a fresh session without your context.
""")


@app.command()
def check(
    build: bool = typer.Option(
        True,
        "--build/--no-build",
        help="Run nix build as part of the preflight (default: on)",
    ),
):
    """Validate yolo-jail config and preflight the build after every config edit."""
    ensure_global_storage()
    workspace = Path.cwd()

    passed = 0
    failed = 0
    warned = 0

    def ok(msg: str):
        nonlocal passed
        passed += 1
        console.print(f"  ✅ {msg}")

    def fail(msg: str, note: str = ""):
        nonlocal failed
        failed += 1
        console.print(f"  ❌ {msg}")
        if note:
            console.print(f"     → {note}")

    def warn(msg: str, note: str = ""):
        nonlocal warned
        warned += 1
        console.print(f"  ⚠️  {msg}")
        if note:
            console.print(f"     → {note}")

    console.print("\n[bold]YOLO Jail Check[/bold]\n")

    console.print("[bold]Config Files[/bold]")
    try:
        user_config = _load_jsonc_file(
            USER_CONFIG_PATH, str(USER_CONFIG_PATH), strict=True
        )
        if USER_CONFIG_PATH.exists():
            ok(f"Parsed user config: {USER_CONFIG_PATH}")
        else:
            ok(f"No user config found: {USER_CONFIG_PATH}")
    except ConfigError as e:
        user_config = {}
        fail(str(e))

    workspace_config_path = workspace / "yolo-jail.jsonc"
    try:
        workspace_config = _load_jsonc_file(
            workspace_config_path, "yolo-jail.jsonc", strict=True
        )
        if workspace_config_path.exists():
            ok(f"Parsed workspace config: {workspace_config_path}")
        else:
            ok("No workspace yolo-jail.jsonc found")
    except ConfigError as e:
        workspace_config = {}
        fail(str(e))
    console.print()

    if failed:
        console.print("[bold]Summary[/bold]")
        console.print(f"  [red]{failed} failed[/red]\n")
        raise typer.Exit(1)

    config = merge_config(user_config, workspace_config)
    repo_root: Optional[Path] = None
    try:
        repo_root = _resolve_repo_root()
        ok(f"Using yolo-jail repo: {repo_root}")
    except SystemExit:
        fail("Could not resolve the yolo-jail repo root")

    console.print("[bold]Merged Configuration[/bold]")
    errors, warnings = _validate_config(config, workspace=workspace)
    runtime, runtime_error = _runtime_for_check(config)
    if runtime_error:
        errors.append(runtime_error)
    elif runtime:
        ok(f"Runtime available: {runtime}")

    if workspace_config_path.exists() and "repo_path" in workspace_config:
        warnings.append(
            "config.repo_path: workspace repo_path is ignored; only the user config uses it"
        )

    for message in warnings:
        warn(message)
    if errors:
        for message in errors:
            fail(message)
        console.print()
        console.print("[bold]Summary[/bold]")
        parts = [f"[red]{failed} failed[/red]"]
        if warned:
            parts.append(f"[yellow]{warned} warnings[/yellow]")
        console.print(f"  {', '.join(parts)}\n")
        raise typer.Exit(1)
    ok("Merged config is semantically valid")
    console.print()

    console.print("[bold]Entrypoint Dry-Run[/bold]")
    try:
        if repo_root is None:
            raise ConfigError("repo root resolution failed")
        if not (repo_root / "src" / "entrypoint.py").exists():
            raise ConfigError(f"entrypoint source not found under {repo_root}")
        _entrypoint_preflight(repo_root, workspace, config)
        ok("Generated Copilot/Gemini jail config in a temp home")
    except (ConfigError, SystemExit) as e:
        fail("Entrypoint preflight failed", str(e))
    console.print()

    console.print("[bold]Image Build[/bold]")
    if build:
        out_link = BUILD_DIR / "check-result"
        if repo_root is None:
            fail("Skipped nix build", "repo root resolution failed")
        else:
            try:
                store_path, build_stderr_tail = _build_image_store_path(
                    repo_root,
                    extra_packages=config.get("packages") or None,
                    out_link=out_link,
                    status_message="[bold blue]Preflighting jail image...",
                )
                if store_path is None:
                    fail(
                        "nix build failed",
                        "\n".join(build_stderr_tail[-10:]) if build_stderr_tail else "",
                    )
                else:
                    ok(f"nix build succeeded: {store_path}")
            finally:
                out_link.unlink(missing_ok=True)
    else:
        warn("Skipped nix build (--no-build)")
    console.print()

    console.print("[bold]Summary[/bold]")
    parts = [f"[green]{passed} passed[/green]"]
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    if warned:
        parts.append(f"[yellow]{warned} warnings[/yellow]")
    console.print(f"  {', '.join(parts)}\n")

    if failed:
        raise typer.Exit(1)


def _config_snapshot_path(workspace: Path) -> Path:
    """Path to the normalized config snapshot for change detection."""
    return workspace / ".yolo" / "config-snapshot.json"


def _check_config_changes(workspace: Path, config: Dict[str, Any]) -> bool:
    """Compare config with last-seen snapshot. Returns True to proceed, False to abort."""
    snapshot_path = _config_snapshot_path(workspace)
    current_json = json.dumps(config, indent=2, sort_keys=True)

    # First run or no snapshot — accept and save
    if not snapshot_path.exists():
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(current_json + "\n")
        return True

    old_json = snapshot_path.read_text().rstrip()
    if old_json == current_json:
        return True

    # Show diff
    diff_lines = list(
        difflib.unified_diff(
            old_json.splitlines(),
            current_json.splitlines(),
            fromfile="previous config",
            tofile="current config",
            lineterm="",
        )
    )

    console.print(
        "\n[bold yellow]⚠  Jail config changed since last run:[/bold yellow]\n"
    )
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            console.print(f"[dim]{line}[/dim]")
        elif line.startswith("+"):
            console.print(f"[green]{line}[/green]")
        elif line.startswith("-"):
            console.print(f"[red]{line}[/red]")
        elif line.startswith("@@"):
            console.print(f"[cyan]{line}[/cyan]")
        else:
            console.print(line)

    if not sys.stdin.isatty():
        console.print(
            "\n[yellow]Non-interactive mode: accepting config changes automatically.[/yellow]"
        )
        snapshot_path.write_text(current_json + "\n")
        return True

    console.print()
    try:
        response = input("Accept these config changes? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[red]Aborted.[/red]")
        return False

    if response in ("y", "yes"):
        snapshot_path.write_text(current_json + "\n")
        return True

    console.print("[red]Config changes rejected. Exiting.[/red]")
    return False


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def run(
    ctx: typer.Context,
    network: str = typer.Option("bridge", help="Container network mode (bridge/host)"),
    new: bool = typer.Option(
        False,
        "--new",
        help="Force a new container even if one already exists for this workspace",
    ),
    profile: bool = typer.Option(
        False,
        "--profile",
        help="Show detailed startup performance timing after command exits",
    ),
):
    """Run the YOLO jail in the current directory."""
    repo_root = _resolve_repo_root()
    workspace = Path.cwd()

    ensure_global_storage()
    try:
        config = load_config(workspace, strict=True)
    except ConfigError as e:
        console.print(f"[bold red]{e}[/bold red]")
        sys.exit(1)
    config_errors, _ = _validate_config(config, workspace=workspace)
    if config_errors:
        console.print("[bold red]Invalid jail config:[/bold red]")
        for message in config_errors:
            console.print(f"  • {message}")
        console.print(
            "\n[dim]Run `yolo check` for a full preflight before restarting.[/dim]"
        )
        sys.exit(1)
    runtime = _runtime(config)

    # Command construction (needed for both exec and run paths)
    full_command = list(ctx.args)

    target_cmd = "bash"
    if full_command:
        # If calling gemini or copilot, inject --yolo
        if full_command[0] in ["gemini", "copilot"]:
            if "--yolo" not in full_command and "-y" not in full_command:
                full_command.insert(1, "--yolo")
        if full_command[0] == "copilot":
            if "--no-auto-update" not in full_command:
                full_command.insert(1, "--no-auto-update")
        target_cmd = shlex.join(full_command)

    # Collect identity env vars early — needed for both exec and run paths
    identity_env = []
    try:
        git_name = (
            subprocess.check_output(
                ["git", "config", "--get", "user.name"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        if git_name:
            identity_env.extend(["-e", f"YOLO_GIT_NAME={git_name}"])
    except Exception:
        pass
    try:
        git_email = (
            subprocess.check_output(
                ["git", "config", "--get", "user.email"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        if git_email:
            identity_env.extend(["-e", f"YOLO_GIT_EMAIL={git_email}"])
    except Exception:
        pass
    try:
        jj_name = (
            subprocess.check_output(
                ["jj", "config", "get", "user.name"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
            .strip('"')
        )
        if jj_name:
            identity_env.extend(["-e", f"YOLO_JJ_NAME={jj_name}"])
    except Exception:
        pass
    try:
        jj_email = (
            subprocess.check_output(
                ["jj", "config", "get", "user.email"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
            .strip('"')
        )
        if jj_email:
            identity_env.extend(["-e", f"YOLO_JJ_EMAIL={jj_email}"])
    except Exception:
        pass

    # Check for existing container BEFORE touching the image.
    # If one is already running we just exec into it — no rebuild needed.
    cname = container_name_for_workspace(workspace)
    existing_cid = None if new else find_running_container(cname, runtime=runtime)

    if existing_cid:
        # Exec into the existing container
        console.print(
            f"[bold cyan]Attaching to existing jail [dim]({cname})[/dim]...[/bold cyan]"
        )
        _tmux_rename_window("JAIL")
        exec_flags = ["-i"]
        if sys.stdout.isatty():
            exec_flags.append("-t")
        docker_cmd = [
            runtime,
            "exec",
            *exec_flags,
            *identity_env,
            cname,
            "yolo-entrypoint",
            target_cmd,
        ]
        # Use subprocess.run (not execvp) so atexit handlers fire for tmux cleanup
        try:
            result = subprocess.run(docker_cmd)
        except FileNotFoundError:
            console.print(
                f"[bold red]Configured runtime '{runtime}' not found on PATH.[/bold red]"
            )
            console.print(
                "[dim]Run `yolo check` to validate runtime availability before restarting.[/dim]"
            )
            sys.exit(1)
        sys.exit(result.returncode)

    # No existing container — build/load the image then start a new one.
    # Check for config changes and get human confirmation
    if not _check_config_changes(workspace, config):
        sys.exit(1)

    # Acquire a workspace-specific lock to prevent two concurrent yolo invocations
    # from racing on build + container creation. The loser waits, then execs into
    # the container the winner created.
    lock_path = GLOBAL_STORAGE / "locks"
    lock_path.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path / f"{cname}.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    except OSError as e:
        console.print(
            f"[dim]Warning: could not acquire workspace lock ({e}); race protection disabled[/dim]"
        )

    # Re-check after acquiring the lock — another process may have started
    # a container while we were waiting.
    if not new:
        raced_cid = find_running_container(cname, runtime=runtime)
        if raced_cid:
            lock_file.close()
            console.print(
                f"[bold cyan]Attaching to jail started by another process [dim]({cname})[/dim]...[/bold cyan]"
            )
            _tmux_rename_window("JAIL")
            exec_flags = ["-i"]
            if sys.stdout.isatty():
                exec_flags.append("-t")
            docker_cmd = [
                runtime,
                "exec",
                *exec_flags,
                *identity_env,
                cname,
                "yolo-entrypoint",
                target_cmd,
            ]
            try:
                result = subprocess.run(docker_cmd)
            except FileNotFoundError:
                console.print(
                    f"[bold red]Configured runtime '{runtime}' not found on PATH.[/bold red]"
                )
                console.print(
                    "[dim]Run `yolo check` to validate runtime availability before restarting.[/dim]"
                )
                sys.exit(1)
            sys.exit(result.returncode)

    import time as _time

    _profile_times = {}
    if profile:
        _profile_times["start"] = _time.monotonic()

    extra_packages = config.get("packages", [])
    mise_tools = _merge_mise_tools(config)
    lsp_servers = config.get("lsp_servers", {})
    mcp_servers = config.get("mcp_servers", {})
    auto_load_image(repo_root, extra_packages=extra_packages or None, runtime=runtime)

    # Resolve host mise path — share the same data dir so venv paths match.
    # Inside a nested jail, YOLO_OUTER_MISE_PATH carries the original host path.
    host_mise = _host_mise_dir()

    if profile:
        _profile_times["image_loaded"] = _time.monotonic()

    # Determine Network Mode
    net_mode = network
    if config.get("network", {}).get("mode"):
        net_mode = config["network"]["mode"]

    # Determine Ports
    publish_args = []
    if net_mode == "bridge" and config.get("network", {}).get("ports"):
        for p in config["network"]["ports"]:
            publish_args.extend(["-p", p])

    # Host port forwarding (host services → container localhost)
    forward_host_ports = []
    if net_mode == "bridge" and config.get("network", {}).get("forward_host_ports"):
        forward_host_ports = config["network"]["forward_host_ports"]

    normalized_blocked = _normalize_blocked_tools(config.get("security"))
    blocked_config_json = json.dumps(normalized_blocked)

    # Process Extra Mounts
    mount_args = []
    mount_descriptions = []
    for mount in config.get("mounts", []):
        # Support "host:container" syntax — split on the LAST colon that precedes
        # an absolute container path (starts with /).  Plain host-only paths like
        # "/home/user/.copilot" or "~/data" fall through to the else branch.
        colon_idx = mount.rfind(":")
        if colon_idx > 0 and mount[colon_idx + 1 : colon_idx + 2] == "/":
            host_path = mount[:colon_idx]
            container_path = mount[colon_idx + 1 :]
        else:
            host_path = mount
            container_path = f"/ctx/{Path(host_path).expanduser().resolve().name}"
        host_path = str(Path(host_path).expanduser().resolve())
        if not Path(host_path).exists():
            console.print(
                f"[yellow]Warning: mount path does not exist, skipping: {host_path}[/yellow]"
            )
            continue
        mount_args.extend(["-v", f"{host_path}:{container_path}:ro"])
        mount_descriptions.append(f"{host_path}:{container_path}")

    # Construct Docker Command
    docker_flags = ["--rm", "-i", "--init", "--name", cname]
    if sys.stdout.isatty():
        docker_flags.append("-t")

    # Per-workspace overlays for workspace-specific state
    ws_state = workspace / ".yolo" / "home"
    ws_state.mkdir(parents=True, exist_ok=True)
    (ws_state / "copilot-sessions").mkdir(exist_ok=True)
    (ws_state / "copilot-command-history").touch()
    (ws_state / "bash_history").touch()
    (ws_state / "gemini-history").mkdir(exist_ok=True)
    (ws_state / "ssh").mkdir(exist_ok=True, mode=0o700)

    docker_cmd = [
        runtime,
        "run",
        *docker_flags,
        "-v",
        f"{workspace}:/workspace",
        # Global home as base (has auth, tools, configs)
        "-v",
        f"{GLOBAL_HOME}:/home/agent",
        # Per-workspace overlays for state that should not leak across workspaces
        "-v",
        f"{ws_state / 'copilot-sessions'}:/home/agent/.copilot/session-state",
        "-v",
        f"{ws_state / 'copilot-command-history'}:/home/agent/.copilot/command-history-state.json",
        "-v",
        f"{ws_state / 'bash_history'}:/home/agent/.bash_history",
        "-v",
        f"{ws_state / 'gemini-history'}:/home/agent/.gemini/history",
        # Per-workspace SSH keys — each project gets its own ~/.ssh/
        "-v",
        f"{ws_state / 'ssh'}:/home/agent/.ssh",
        "-v",
        f"{host_mise}:{host_mise}",
        "--tmpfs",
        "/tmp",
        "--shm-size=2g",
        "-e",
        "JAIL_HOME=/home/agent",
        "-e",
        "NPM_CONFIG_PREFIX=/home/agent/.npm-global",
        "-e",
        "GOPATH=/home/agent/go",
        "-e",
        f"MISE_DATA_DIR={host_mise}",
        "-e",
        # Use a per-container cache dir so mise lockfiles don't contend with
        # the host/outer-jail's locks (shared /home/agent would otherwise share
        # ~/.cache/mise/lockfiles/, causing deadlocks in nested jails).
        "MISE_CACHE_DIR=/tmp/mise-cache",
        "-e",
        "MISE_TRUST=1",
        "-e",
        "MISE_YES=1",
        "-e",
        "COPILOT_ALLOW_ALL=true",
        "-e",
        "LD_LIBRARY_PATH=/lib:/usr/lib",
        "-e",
        "HOME=/home/agent",
        # EDITOR=cat prevents agents from getting stuck in interactive editors.
        # VISUAL=nvim is used by Copilot ctrl-g (checks COPILOT_EDITOR > VISUAL > EDITOR).
        # These must be container-level env vars, not just in .bashrc, because
        # Copilot runs as a non-interactive process that doesn't source .bashrc.
        "-e",
        "EDITOR=cat",
        "-e",
        "VISUAL=nvim",
        "-e",
        "PAGER=cat",
        "-e",
        "GIT_PAGER=cat",
        "-e",
        f"YOLO_BLOCK_CONFIG={blocked_config_json}",
        "-e",
        f"YOLO_HOST_DIR={workspace}",
        "-e",
        "OVERMIND_SOCKET=/tmp/overmind.sock",
        "-e",
        f"YOLO_MISE_TOOLS={json.dumps(mise_tools)}",
        "-e",
        f"YOLO_LSP_SERVERS={json.dumps(lsp_servers)}",
        "-e",
        f"YOLO_MCP_SERVERS={json.dumps(mcp_servers)}",
        "-e",
        f"YOLO_RUNTIME={runtime}",
        "-e",
        "YOLO_REPO_ROOT=/opt/yolo-jail",
        "--workdir",
        "/workspace",
        # Mount yolo-jail repo for in-jail CLI (yolo --help, nested jailing)
        "-v",
        f"{repo_root}:/opt/yolo-jail:ro",
    ]

    # Docker needs explicit UID mapping; podman rootless maps container root to host user
    if runtime == "docker":
        docker_cmd.extend(["-u", f"{os.getuid()}:{os.getgid()}"])

    # Detect if we're already inside a container
    in_container = Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()

    # Podman: enable nested container support (rootless podman-in-podman)
    # When running on the host, use UID/GID mapping to create a user namespace.
    # When already inside a container, share the parent's user namespace instead
    # to avoid kernel restrictions on doubly-nested user namespaces.
    if runtime == "podman":
        if in_container:
            # Inside a container: share parent's user namespace
            docker_cmd.extend(
                [
                    "--security-opt",
                    "label=disable",
                    "--userns",
                    "host",
                ]
            )
        else:
            # On host: create user namespace with UID/GID mapping for nesting
            docker_cmd.extend(
                [
                    "--security-opt",
                    "label=disable",
                    "--device",
                    "/dev/fuse",
                    "--uidmap",
                    "0:0:1",
                    "--uidmap",
                    "1:1:65536",
                    "--gidmap",
                    "0:0:1",
                    "--gidmap",
                    "1:1:65536",
                    "--cap-add",
                    "SYS_ADMIN",
                    "--cap-add",
                    "MKNOD",
                ]
            )

    # Mount host nix daemon socket + store so nix builds work inside the jail.
    # NIX_REMOTE=daemon forces nix to use the host daemon (which has nixbld users)
    # instead of trying local store access (which fails on UID mapping/permissions).
    nix_socket = Path("/nix/var/nix/daemon-socket")
    nix_store = Path("/nix/store")
    if nix_socket.exists() and nix_store.exists():
        docker_cmd.extend(
            [
                "-v",
                f"{nix_socket}:{nix_socket}",
                "-v",
                f"{nix_store}:{nix_store}:ro",
                "-e",
                "NIX_REMOTE=daemon",
            ]
        )

    # Podman rootless uses pasta networking by default (no nftables needed).
    # Only pass --net explicitly for non-default modes like "host".
    # Inside a container, always use host networking (netavark can't create
    # network namespaces without NET_ADMIN).
    if runtime == "podman" and in_container:
        docker_cmd.append("--net=host")
    elif net_mode != "bridge" or runtime == "docker":
        docker_cmd.append(f"--net={net_mode}")

    # Docker bridge: add host.internal → host-gateway so socat (and agents)
    # can reach host services.  Podman does this automatically.
    if runtime == "docker" and net_mode == "bridge":
        docker_cmd.extend(["--add-host", "host.internal:host-gateway"])

    # Pass identity env vars (git + jj) collected earlier
    docker_cmd.extend(identity_env)

    # Propagate host global gitignore into the jail
    # (We don't mount ~/.gitconfig to avoid credential leaks, but gitignore is safe)
    try:
        excludes_file = (
            subprocess.check_output(
                ["git", "config", "--global", "--get", "core.excludesFile"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
        if excludes_file:
            excludes_path = Path(excludes_file).expanduser()
        else:
            excludes_path = Path.home() / ".config" / "git" / "ignore"
    except Exception:
        excludes_path = Path.home() / ".config" / "git" / "ignore"
    if excludes_path.is_file():
        docker_cmd.extend(["-v", f"{excludes_path}:/home/agent/.config/git/ignore:ro"])
        docker_cmd.extend(
            ["-e", "YOLO_GLOBAL_GITIGNORE=/home/agent/.config/git/ignore"]
        )

    docker_cmd.extend(publish_args)
    docker_cmd.extend(mount_args)

    # Host port forwarding via Unix sockets
    socket_dir = None
    if forward_host_ports:
        socket_dir = Path(f"/tmp/yolo-fwd-{cname}")
        docker_cmd.extend(["-v", f"{socket_dir}:/tmp/yolo-fwd:rw"])
        docker_cmd.extend(
            ["-e", f"YOLO_FORWARD_HOST_PORTS={json.dumps(forward_host_ports)}"]
        )

    # Device passthrough from config
    for dev in config.get("devices", []):
        if isinstance(dev, str):
            # Raw device path: "/dev/bus/usb/001/004"
            if not Path(dev).exists():
                console.print(
                    f"[yellow]Warning: device {dev} not found — skipping[/yellow]"
                )
                continue
            docker_cmd.extend(["--device", dev])
        elif isinstance(dev, dict):
            if "usb" in dev:
                # Resolve USB vendor:product ID to /dev/bus/usb path
                usb_id = dev["usb"]
                desc = dev.get("description", usb_id)
                try:
                    result = subprocess.run(
                        ["lsusb", "-d", usb_id],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode != 0 or not result.stdout.strip():
                        console.print(
                            f"[yellow]Warning: USB device {desc} ({usb_id}) not found — skipping[/yellow]"
                        )
                        continue
                    # Parse: "Bus 001 Device 004: ID 0bda:2838 ..."
                    line = result.stdout.strip().split("\n")[0]
                    parts = line.split()
                    bus = parts[1]  # "001"
                    device = parts[3].rstrip(":")  # "004"
                    dev_path = f"/dev/bus/usb/{bus}/{device}"
                    if not Path(dev_path).exists():
                        console.print(
                            f"[yellow]Warning: USB device {desc} found by lsusb but {dev_path} missing — skipping[/yellow]"
                        )
                        continue
                    docker_cmd.extend(["--device", dev_path])
                    console.print(f"[dim]USB device: {desc} → {dev_path}[/dim]")
                except FileNotFoundError:
                    console.print(
                        "[yellow]Warning: lsusb not found — cannot resolve USB device IDs[/yellow]"
                    )
                except Exception as e:
                    console.print(
                        f"[yellow]Warning: USB device resolution failed for {usb_id}: {e}[/yellow]"
                    )
            elif "cgroup_rule" in dev:
                docker_cmd.extend(["--device-cgroup-rule", dev["cgroup_rule"]])

    # Copy host nvim config (resolving symlinks) so ctrl-g uses the user's config.
    # We copy instead of bind-mounting because dotfile managers (stow, etc.) create
    # symlinks like init.lua -> ~/.dotfiles/... which break inside the container.
    host_nvim_config = Path.home() / ".config" / "nvim"
    if host_nvim_config.is_dir():
        jail_nvim_config = GLOBAL_STORAGE / "home" / ".config" / "nvim"
        try:
            if jail_nvim_config.exists():
                shutil.rmtree(jail_nvim_config)
            jail_nvim_config.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                host_nvim_config,
                jail_nvim_config,
                symlinks=False,
                ignore_dangling_symlinks=True,
            )
        except (OSError, shutil.Error) as e:
            console.print(f"[yellow]Warning: could not copy nvim config: {e}[/yellow]")

    # Shadow workspace .vscode/mcp.json so agents use only our jail MCP config
    vscode_mcp = workspace / ".vscode" / "mcp.json"
    if vscode_mcp.exists():
        docker_cmd.extend(["-v", "/dev/null:/workspace/.vscode/mcp.json:ro"])

    # Shadow workspace .overmind.sock so host overmind doesn't leak into the jail
    overmind_sock = workspace / ".overmind.sock"
    if overmind_sock.exists():
        docker_cmd.extend(["-v", "/dev/null:/workspace/.overmind.sock:ro"])

    # Pass original host mise path for nested jail re-mounting
    docker_cmd.extend(["-e", f"YOLO_OUTER_MISE_PATH={host_mise}"])

    # Mount host user-level copilot/gemini skills so they're available in the jail
    host_gemini_skills = Path.home() / ".gemini" / "skills"
    host_dotfiles_skills = Path.home() / ".dotfiles" / "gemini" / "skills"

    if host_gemini_skills.exists() and host_gemini_skills.is_dir():
        docker_cmd.extend(["-v", f"{host_gemini_skills}:/ctx/host-gemini-skills:ro"])
        docker_cmd.extend(["-e", "YOLO_HOST_GEMINI_SKILLS=/ctx/host-gemini-skills"])

        if host_dotfiles_skills.exists() and host_dotfiles_skills.is_dir():
            docker_cmd.extend(
                ["-v", f"{host_dotfiles_skills}:{host_dotfiles_skills}:ro"]
            )

    # Generate per-workspace AGENTS.md (separate for Copilot and Gemini to
    # respect user-level ~/.copilot/AGENTS.md vs ~/.gemini/AGENTS.md)
    agents_path = generate_agents_md(
        cname,
        workspace,
        normalized_blocked,
        mount_descriptions,
        net_mode=net_mode,
        runtime=runtime,
        forward_host_ports=forward_host_ports or None,
        mcp_servers=mcp_servers or None,
    )
    docker_cmd.extend(
        ["-v", f"{agents_path / 'AGENTS-copilot.md'}:/home/agent/.copilot/AGENTS.md:ro"]
    )
    docker_cmd.extend(
        ["-v", f"{agents_path / 'AGENTS-gemini.md'}:/home/agent/.gemini/AGENTS.md:ro"]
    )

    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    if profile:
        docker_cmd.extend(["-e", "YOLO_PROFILE=1"])

    docker_cmd.append(JAIL_IMAGE)
    docker_cmd.append("yolo-entrypoint")

    # If mise.toml exists in workspace, trust it.
    # Then ensure all tools (global + local) are ready.
    setup_script = "YOLO_BYPASS_SHIMS=1 sh -c '(if [ -f mise.toml ]; then mise trust; fi) && mise install && ~/.yolo-bootstrap.sh && ~/.yolo-venv-precreate.sh'"
    # After setup, activate mise so tool paths (copilot, gemini, etc.) are in PATH.
    # We use `mise env` (one-time activation) rather than `mise hook-env` (continuous
    # shell integration) because hook-env deadlocks when it needs to create a venv:
    # it holds a lock, spawns `uv` via the mise shim (which IS mise), and the shim
    # tries to acquire the same lock → deadlock.
    mise_activate = 'eval "$(mise env -s bash)" 2>/dev/null'
    # Use && for fail-fast: if provisioning fails, don't proceed with broken env
    if profile:
        # Wrap each phase with timing output for profiling
        final_internal_cmd = (
            "exec 3>&2; "  # save stderr
            f"_t0=$(date +%s%N); {setup_script} >/dev/null 2>&1; "
            "_t1=$(date +%s%N); "
            f"{mise_activate}; "
            "_t2=$(date +%s%N); "
            f"{target_cmd}; _rc=$?; "
            "_t3=$(date +%s%N); "
            # Print profile report to stderr
            "echo '' >&3; echo '=== YOLO Jail Profile ===' >&3; "
            "echo '' >&3; echo '--- Entrypoint (config generation) ---' >&3; "
            # Extract only the LAST run from the perf log (separated by === markers)
            'awk \'/^=== YOLO/{buf=""} {buf=buf $0 "\\n"} END{printf "%s", buf}\' ~/.yolo-perf.log >&3 2>/dev/null; '
            "echo '' >&3; echo '--- Container setup ---' >&3; "
            "printf '  mise install + bootstrap: %s\\n' \"$(( (_t1 - _t0) / 1000000 ))ms\" >&3; "
            "printf '  mise hook-env:            %s\\n' \"$(( (_t2 - _t1) / 1000000 ))ms\" >&3; "
            "printf '  command execution:        %s\\n' \"$(( (_t3 - _t2) / 1000000 ))ms\" >&3; "
            "printf '  total in-container:       %s\\n' \"$(( (_t3 - _t0) / 1000000 ))ms\" >&3; "
            "echo '' >&3; "
            # Also show mise shim vs direct node timing
            "echo '--- Node path comparison ---' >&3; "
            "_n0=$(date +%s%N); /bin/node --version >/dev/null 2>&1; _n1=$(date +%s%N); "
            "printf '  /bin/node:        %sms\\n' \"$(( (_n1 - _n0) / 1000000 ))\" >&3; "
            "_n2=$(date +%s%N); /mise/shims/node --version >/dev/null 2>&1; _n3=$(date +%s%N); "
            "printf '  /mise/shims/node: %sms\\n' \"$(( (_n3 - _n2) / 1000000 ))\" >&3; "
            "echo '' >&3; "
            "exit $_rc"
        )
    else:
        final_internal_cmd = (
            f"{setup_script} >/dev/null 2>&1 && {mise_activate}; {target_cmd}"
        )

    docker_cmd.append(final_internal_cmd)

    write_container_tracking(cname, workspace)
    _tmux_rename_window("JAIL")

    # Start host-side port forwarding BEFORE the container so socket files
    # exist when entrypoint.py starts the container-side socat.
    socat_procs: List[subprocess.Popen] = []
    if socket_dir:
        socat_procs = start_host_port_forwarding(forward_host_ports, cname, socket_dir)

    # Use Popen so we can release the workspace lock once the container is
    # confirmed running.  Any concurrent yolo process waiting on the lock will
    # re-check and find our container, then exec into it.
    try:
        proc = subprocess.Popen(docker_cmd)
    except FileNotFoundError:
        console.print(
            f"[bold red]Configured runtime '{runtime}' not found on PATH.[/bold red]"
        )
        console.print(
            "[dim]Run `yolo check` to validate runtime availability before restarting.[/dim]"
        )
        cleanup_port_forwarding(socat_procs, socket_dir)
        lock_file.close()
        sys.exit(1)
    for _ in range(20):
        if find_running_container(cname, runtime=runtime):
            break
        _time.sleep(0.25)
    lock_file.close()

    proc.wait()
    result = proc

    # Clean up host-side socat processes and socket directory
    cleanup_port_forwarding(socat_procs, socket_dir)

    if profile and _profile_times:
        _profile_times["container_exited"] = _time.monotonic()
        start = _profile_times["start"]
        err = Console(stderr=True)
        err.print("\n[bold cyan]--- Host-side timing ---[/bold cyan]")
        err.print(
            f"  Image build/load:   {_profile_times.get('image_loaded', start) - start:.3f}s"
        )
        err.print(
            f"  Total (host-side):  {_profile_times['container_exited'] - start:.3f}s\n"
        )

    sys.exit(result.returncode)


@app.command()
def ps():
    """List running YOLO jail containers."""
    runtime = _runtime()
    result = subprocess.run(
        [
            runtime,
            "ps",
            "--filter",
            "name=^yolo-",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        typer.echo(result.stdout.strip())
        # Show workspace mappings, clean up stale tracking files
        for tracking_file in (
            sorted(CONTAINER_DIR.iterdir()) if CONTAINER_DIR.exists() else []
        ):
            name = tracking_file.name
            if find_running_container(name, runtime=runtime):
                workspace_path = tracking_file.read_text().strip()
                typer.echo(f"  {name} → {workspace_path}")
            else:
                cleanup_container_tracking(name)
    else:
        typer.echo("No running jails.")
        # Clean up all stale tracking files
        if CONTAINER_DIR.exists():
            for tracking_file in CONTAINER_DIR.iterdir():
                cleanup_container_tracking(tracking_file.name)


@app.command()
def doctor():
    """Check your YOLO Jail setup and diagnose common problems."""
    passed = 0
    failed = 0
    warned = 0

    def ok(msg: str):
        nonlocal passed
        passed += 1
        console.print(f"  ✅ {msg}")

    def fail(msg: str, fix: str = ""):
        nonlocal failed
        failed += 1
        console.print(f"  ❌ {msg}")
        if fix:
            console.print(f"     → {fix}")

    def warn(msg: str, note: str = ""):
        nonlocal warned
        warned += 1
        console.print(f"  ⚠️  {msg}")
        if note:
            console.print(f"     → {note}")

    console.print("\n[bold]YOLO Jail Doctor[/bold]\n")

    # 1. Container runtime
    console.print("[bold]Container Runtime[/bold]")
    runtime = None
    for rt in ("podman", "docker"):
        path = shutil.which(rt)
        if path:
            try:
                result = subprocess.run(
                    [rt, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                version = result.stdout.strip().split("\n")[0]
                ok(f"{rt}: {version}")
                if runtime is None:
                    runtime = rt
            except Exception as e:
                fail(f"{rt} found but not working: {e}")
        else:
            if rt == "docker":
                pass  # Only warn if neither found
            else:
                pass  # Check after loop
    if runtime is None:
        fail("No container runtime found", "Install podman or docker")
    console.print()

    # 2. Nix
    console.print("[bold]Nix[/bold]")
    nix_path = shutil.which("nix")
    if nix_path:
        try:
            result = subprocess.run(
                ["nix", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            ok(f"nix: {result.stdout.strip()}")
        except Exception as e:
            fail(f"nix found but not working: {e}")
    else:
        fail("nix not found", "Install Nix: https://nixos.org/download/")

    # Check flake.nix exists
    try:
        repo = _resolve_repo_root()
        flake = repo / "flake.nix"
        ok(f"flake.nix found: {flake}")
    except SystemExit:
        warn("yolo-jail repo root not found (set repo_path in user config)")
    console.print()

    # 3. Global storage
    console.print("[bold]Global Storage[/bold]")
    for name, path in [
        ("Home", GLOBAL_HOME),
        ("Mise", GLOBAL_MISE),
        ("Containers", CONTAINER_DIR),
        ("Agents", AGENTS_DIR),
        ("Build", BUILD_DIR),
    ]:
        if path.exists():
            ok(f"{name}: {path}")
        else:
            warn(f"{name} directory missing: {path}", "Will be created on first run")
    console.print()

    # 4. Configuration
    console.print("[bold]Configuration[/bold]")
    if USER_CONFIG_PATH.exists():
        try:
            with open(USER_CONFIG_PATH) as f:
                pyjson5.load(f)
            ok(f"User config: {USER_CONFIG_PATH}")
        except Exception as e:
            fail(f"User config invalid: {e}", f"Edit {USER_CONFIG_PATH}")
    else:
        warn(
            "No user config",
            f"Run 'yolo init-user-config' to create {USER_CONFIG_PATH}",
        )

    workspace_config = Path.cwd() / "yolo-jail.jsonc"
    if workspace_config.exists():
        try:
            with open(workspace_config) as f:
                pyjson5.load(f)
            ok(f"Workspace config: {workspace_config}")
        except Exception as e:
            fail(f"Workspace config invalid: {e}", f"Edit {workspace_config}")
    else:
        warn("No workspace config", "Run 'yolo init' to create yolo-jail.jsonc")
    console.print()

    # 5. Docker/Podman image
    console.print("[bold]Container Image[/bold]")
    if runtime:
        try:
            result = subprocess.run(
                [
                    runtime,
                    "images",
                    JAIL_IMAGE,
                    "--format",
                    "{{.Repository}}:{{.Tag}} ({{.Size}})",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            images = result.stdout.strip()
            if images:
                ok(f"Image loaded: {images.split(chr(10))[0]}")
            else:
                warn(
                    f"Image '{JAIL_IMAGE}' not loaded",
                    "Run 'yolo' once to build and load the image",
                )
        except Exception as e:
            warn(f"Could not check image: {e}")
    else:
        warn("Skipped (no container runtime)")
    console.print()

    # 6. Running containers
    console.print("[bold]Running Jails[/bold]")
    if runtime:
        try:
            result = subprocess.run(
                [runtime, "ps", "--filter", "name=^yolo-", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            containers = [c for c in result.stdout.strip().split("\n") if c]
            if containers:
                ok(f"{len(containers)} jail(s) running: {', '.join(containers)}")
            else:
                ok("No jails currently running")
        except Exception:
            warn("Could not check running containers")
    console.print()

    # Summary
    console.print("[bold]Summary[/bold]")
    parts = [f"[green]{passed} passed[/green]"]
    if failed:
        parts.append(f"[red]{failed} failed[/red]")
    if warned:
        parts.append(f"[yellow]{warned} warnings[/yellow]")
    console.print(f"  {', '.join(parts)}\n")

    if failed:
        sys.exit(1)


def main():
    """Entry point for the `yolo` console script.

    Handles visual jail indicator (kitty tab or tmux pane border) and routes to
    the typer CLI.  Detection priority: kitty-native > tmux > neither.
    YOLO_NO_TMUX=1 skips all tmux interactions (useful in kitty-only setups).
    """
    import atexit

    # Kitty-native mode takes priority over tmux
    if os.environ.get("KITTY_PID") and not os.environ.get("TMUX"):
        restore = _kitty_setup_jail_tab()
    else:
        restore = _tmux_setup_jail_pane()
    if restore:
        atexit.register(restore)

    app()


if __name__ == "__main__":
    main()
