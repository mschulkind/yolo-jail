import os
import subprocess
import sys
import json
import shlex
import shutil
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import typer
import pyjson5

app = typer.Typer(invoke_without_command=True)


@app.callback()
def _default(ctx: typer.Context):
    """YOLO Jail: Secure container for AI agents."""
    if ctx.invoked_subcommand is None:
        # No subcommand → default to `run` (interactive shell)
        ctx.invoke(run)

JAIL_IMAGE = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"
CONTAINER_DIR = GLOBAL_STORAGE / "containers"
AGENTS_DIR = GLOBAL_STORAGE / "agents"
USER_CONFIG_PATH = Path.home() / ".config" / "yolo-jail" / "config.jsonc"

from rich.console import Console
from rich.status import Status

console = Console()

def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)
    CONTAINER_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)


def _tmux_rename_window(name: str):
    """Rename the current tmux window. No-op if not in tmux."""
    if os.environ.get("TMUX"):
        try:
            subprocess.run(["tmux", "rename-window", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _tmux_setup_jail_pane():
    """Set tmux pane border indicators for the jail. Returns cleanup function."""
    if not os.environ.get("TMUX") or not sys.stdin.isatty():
        return None

    pane = os.environ.get("TMUX_PANE", "")
    jail_dir = Path.cwd().name

    def _tmux_opt(opt):
        try:
            r = subprocess.run(
                ["tmux", "show-option", "-pt", pane, opt],
                capture_output=True, text=True,
            )
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    def _tmux_set(opt, val):
        try:
            subprocess.run(
                ["tmux", "set-option", "-pt", pane, opt, val],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _tmux_unset(opt):
        try:
            subprocess.run(
                ["tmux", "set-option", "-put", pane, opt],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    # Save old state
    old = {opt: _tmux_opt(opt) for opt in [
        "pane-border-style", "pane-active-border-style",
        "pane-border-status", "pane-border-format",
    ]}
    old_window = None
    old_auto_rename = None
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "#{window_name}"],
                           capture_output=True, text=True)
        old_window = r.stdout.strip() if r.returncode == 0 else None
        r = subprocess.run(["tmux", "show-window-option", "-v", "automatic-rename"],
                           capture_output=True, text=True)
        old_auto_rename = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        pass

    # Set jail indicators
    _tmux_set("pane-border-style", "fg=red,bold")
    _tmux_set("pane-active-border-style", "fg=red,bold")
    _tmux_set("pane-border-status", "bottom")
    _tmux_set("pane-border-format", f" 🔒 JAIL {jail_dir} ")
    try:
        subprocess.run(["tmux", "set-window-option", "automatic-rename", "off"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "rename-window", "JAIL"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    def restore():
        for opt, val in old.items():
            if val:
                # val is like "pane-border-style fg=red,bold" — use eval-style restore
                try:
                    subprocess.run(
                        ["tmux", f"set-option", "-pt", pane, opt, val.split()[-1]],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    _tmux_unset(opt)
            else:
                _tmux_unset(opt)
        if old_window:
            try:
                subprocess.run(["tmux", "rename-window", old_window],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        if old_auto_rename == "on":
            try:
                subprocess.run(["tmux", "set-window-option", "automatic-rename", "on"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
    console.print("[bold red]No container runtime found. Install podman or docker.[/bold red]")
    sys.exit(1)


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic container name from workspace path."""
    h = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:12]
    return f"yolo-{h}"


def find_running_container(name: str, runtime: str = "docker") -> Optional[str]:
    """Return container ID if a container with this name is running, else None."""
    result = subprocess.run(
        [runtime, "ps", "-q", "--filter", f"name=^/{name}$"],
        capture_output=True, text=True,
    )
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


def generate_agents_md(
    cname: str,
    workspace: Path,
    blocked_tools: List[Dict[str, str]],
    mount_descriptions: List[str],
    net_mode: str = "bridge",
    runtime: str = "podman",
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
        network_line = "- **Network**: Bridge mode (Docker). Discover gateway IP: `ip route | awk '/default/ {print $3}'` (typically 172.17.0.1). Use that IP to reach the host."

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
        "",
        "## Available Tools",
        "",
        "Standard CLI tools: git, rg (ripgrep), fd, bat, jq, nvim, curl, wget, strace, gh",
        "Runtimes: Node.js 22, Python 3.13, Go (managed by mise)",
        "MCP Servers: chrome-devtools (headless Chromium), sequential-thinking",
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

    lines.extend([
        "## Limitations",
        "",
        "- **No internet restrictions** but no host credentials (no ~/.ssh, no ~/.gitconfig).",
        "- **No pagers**: PAGER=cat, GIT_PAGER=cat. Do not pipe to less/more.",
        "- **Read-only mounts**: Context mounts under `/ctx/` are read-only.",
        "- **No sudo/root**: You run as a mapped host user with no privilege escalation.",
        "- **No git push/pull**: No GitHub credentials are available. Do not attempt `gh auth login` or SSH-based git operations.",
        "",
    ])

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

def auto_load_image(repo_root: Path, extra_packages: List[str] = None, runtime: str = "docker"):
    """Cheaply check if the nix image needs to be reloaded into the container runtime."""
    # Per-runtime sentinel so docker and podman each track their own loaded image
    sentinel = repo_root / f".last-load-{runtime}"
    
    # 1. Build the image (cheap if no changes)
    build_env = os.environ.copy()
    if extra_packages:
        build_env["YOLO_EXTRA_PACKAGES"] = json.dumps(extra_packages)
    
    with console.status("[bold blue]Checking jail image...", spinner="dots"):
        try:
            subprocess.run(
                ["nix", "--extra-experimental-features", "nix-command flakes", "build", ".#dockerImage", "--impure", "--out-link", ".run-result"],
                cwd=repo_root, check=True, capture_output=True,
                env=build_env,
            )
        except subprocess.CalledProcessError as e:
            console.print(f"[yellow]Warning: Automatic nix build failed: {e.stderr.decode()}[/yellow]")
            # If the image already exists in the runtime (e.g. pre-loaded inside a jail),
            # we can still proceed — just skip the load step.
            check = subprocess.run(
                [runtime, "image", "inspect", JAIL_IMAGE],
                capture_output=True,
            )
            if check.returncode == 0:
                console.print(f"[yellow]Using existing {JAIL_IMAGE} image.[/yellow]")
                return
            console.print(f"[bold red]No existing {JAIL_IMAGE} image found. Cannot start jail.[/bold red]")
            return

    # 2. Check if the store path has changed
    current_path = (repo_root / ".run-result").resolve()
    last_path = None
    if sentinel.exists():
        last_path = sentinel.read_text().strip()

    if str(current_path) != last_path:
        console.print("[bold green]Detected changes in jail config. Loading new image...[/bold green]")
        
        try:
            with open(repo_root / ".run-result", "rb") as image_file:
                # Use Popen to stream output line by line for the fancy status
                process = subprocess.Popen(
                    [runtime, "load"],
                    stdin=image_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                
                with console.status(f"[bold cyan]Loading into {runtime}...", spinner="bouncingBar") as status:
                    if process.stdout:
                        for line in iter(process.stdout.readline, ""):
                            clean_line = line.strip()
                            if clean_line:
                                # Show the last line of docker load (e.g. "Loaded image: ...")
                                status.update(f"[bold cyan]Loading: [dim]{clean_line}[/dim]")
                                last_line = clean_line
                
                process.wait()
                if process.returncode != 0:
                    console.print(f"[bold red]Error loading {runtime} image.[/bold red]")
                else:
                    console.print(f"[bold green]Successfully {last_line.lower() if 'last_line' in locals() else 'loaded image'}[/bold green]")
                    sentinel.write_text(str(current_path))
        except Exception as e:
            console.print(f"[bold red]Error loading {runtime} image: {e}[/bold red]")
    
    # Cleanup temp link
    (repo_root / ".run-result").unlink(missing_ok=True)

def _load_jsonc_file(path: Path, label: str) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            parsed = pyjson5.load(f)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
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
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            result[key] = _merge_lists(result[key], value)
        else:
            result[key] = value
    return result

def load_config() -> Dict[str, Any]:
    user_config = _load_jsonc_file(USER_CONFIG_PATH, str(USER_CONFIG_PATH))
    workspace_config = _load_jsonc_file(Path.cwd() / "yolo-jail.jsonc", "yolo-jail.jsonc")
    return merge_config(user_config, workspace_config)

@app.command()
def init():
    """Initialize a yolo-jail.jsonc configuration file in the current directory."""
    config_path = Path.cwd() / "yolo-jail.jsonc"
    if config_path.exists():
        typer.echo("yolo-jail.jsonc already exists.")
        return

    content = """{
  // Container runtime: "podman" or "docker" (also settable via YOLO_RUNTIME env var)
  // "runtime": "podman",

  // Extra nix packages to include in the jail image.
  // Names must match nixpkgs attribute names (search at https://search.nixos.org/packages).
  // The image rebuilds only when this list changes.
  // "packages": ["postgresql", "redis", "awscli2"],

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
  },
  // Extra host paths to mount read-only into the jail for context.
  // Each entry is a host path (mounted at /ctx/<basename>) or "host:container".
  // "mounts": [
  //   "~/code/other-repo",
  //   "~/code/shared-lib:/ctx/shared-lib"
  // ]
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

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    network: str = typer.Option("bridge", help="Container network mode (bridge/host)"),
    new: bool = typer.Option(False, "--new", help="Force a new container even if one already exists for this workspace"),
):
    """Run the YOLO jail in the current directory."""
    # Find repo root to locate sentinel file
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = Path.cwd()
    
    ensure_global_storage()
    config = load_config()
    runtime = _runtime(config)

    # Command construction (needed for both exec and run paths)
    full_command = list(ctx.args)

    target_cmd = "bash"
    if full_command:
        # If calling gemini or copilot, inject --yolo
        if full_command[0] in ["gemini", "copilot"]:
            if "--yolo" not in full_command and "-y" not in full_command:
                full_command.insert(1, "--yolo")
        target_cmd = shlex.join(full_command)

    # Check for existing container BEFORE touching the image.
    # If one is already running we just exec into it — no rebuild needed.
    cname = container_name_for_workspace(workspace)
    existing_cid = None if new else find_running_container(cname, runtime=runtime)

    if existing_cid:
        # Exec into the existing container
        console.print(f"[bold cyan]Attaching to existing jail [dim]({cname})[/dim]...[/bold cyan]")
        _tmux_rename_window("JAIL")
        exec_flags = ["-i"]
        if sys.stdout.isatty():
            exec_flags.append("-t")
        docker_cmd = [
            runtime, "exec", *exec_flags,
            cname,
            "yolo-entrypoint", target_cmd,
        ]
        # Use subprocess.run (not execvp) so atexit handlers fire for tmux cleanup
        result = subprocess.run(docker_cmd)
        sys.exit(result.returncode)

    # No existing container — build/load the image then start a new one.
    extra_packages = config.get("packages", [])
    auto_load_image(repo_root, extra_packages=extra_packages or None, runtime=runtime)

    # Determine Network Mode
    net_mode = network
    if config.get("network", {}).get("mode"):
        net_mode = config["network"]["mode"]
    
    # Determine Ports
    publish_args = []
    if net_mode == "bridge" and config.get("network", {}).get("ports"):
        for p in config["network"]["ports"]:
            publish_args.extend(["-p", p])

    # Process Blocked Tools for the Container
    security_section = config.get("security", {})
    if security_section is None: 
        security_section = {}
    
    raw_blocked = security_section.get("blocked_tools", ["grep", "find"])
    if raw_blocked is None:
        raw_blocked = ["grep", "find"]

    # Default messages and suggestions for standard tools
    default_messages = {
        "grep": {
            "message": "grep is blocked to prevent unintended recursive searches. Use ripgrep (rg) or other targeted tools.",
            "suggestion": "Try: rg <pattern> [file]"
        },
        "find": {
            "message": "find is blocked to prevent unintended recursive searches. Use fd for a faster, more intuitive alternative.",
            "suggestion": "Try: fd <pattern>"
        }
    }

    normalized_blocked = []
    
    for tool in raw_blocked:
        if isinstance(tool, str):
            tool_dict = {"name": tool}
            # Add default message if available
            if tool in default_messages:
                tool_dict.update(default_messages[tool])
            normalized_blocked.append(tool_dict)
        elif isinstance(tool, dict) and "name" in tool:
            normalized_blocked.append(tool)
            
    blocked_config_json = json.dumps(normalized_blocked)

    # Process Extra Mounts
    mount_args = []
    mount_descriptions = []
    for mount in config.get("mounts", []):
        if ":" in mount and not mount.startswith("~") and not mount.startswith("/"):
            host_path, container_path = mount.split(":", 1)
        else:
            host_path = mount
            container_path = f"/ctx/{Path(host_path).expanduser().resolve().name}"
        host_path = str(Path(host_path).expanduser().resolve())
        if not Path(host_path).exists():
            console.print(f"[yellow]Warning: mount path does not exist, skipping: {host_path}[/yellow]")
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

    docker_cmd = [
        runtime, "run", *docker_flags,
        "-v", f"{workspace}:/workspace",
        # Global home as base (has auth, tools, configs)
        "-v", f"{GLOBAL_HOME}:/home/agent",
        # Per-workspace overlays for state that should not leak across workspaces
        "-v", f"{ws_state / 'copilot-sessions'}:/home/agent/.copilot/session-state",
        "-v", f"{ws_state / 'copilot-command-history'}:/home/agent/.copilot/command-history-state.json",
        "-v", f"{ws_state / 'bash_history'}:/home/agent/.bash_history",
        "-v", f"{ws_state / 'gemini-history'}:/home/agent/.gemini/history",
        "-v", f"{GLOBAL_MISE}:/mise",
        "--tmpfs", "/tmp",
        "--shm-size=2g",
        "-e", "JAIL_HOME=/home/agent",
        "-e", "NPM_CONFIG_PREFIX=/home/agent/.npm-global",
        "-e", "GOPATH=/home/agent/go",
        "-e", "MISE_DATA_DIR=/mise",
        "-e", "MISE_TRUST=1",
        "-e", "MISE_YES=1",
        "-e", "COPILOT_ALLOW_ALL=true",
        "-e", "LD_LIBRARY_PATH=/lib:/usr/lib",
        "-e", "HOME=/home/agent",
        "-e", f"YOLO_BLOCK_CONFIG={blocked_config_json}",
        "-e", f"YOLO_HOST_DIR={workspace}",
        "-e", "OVERMIND_SOCKET=/tmp/overmind.sock",
        "--workdir", "/workspace",
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
            docker_cmd.extend([
                "--security-opt", "label=disable",
                "--userns", "host",
            ])
        else:
            # On host: create user namespace with UID/GID mapping for nesting
            docker_cmd.extend([
                "--security-opt", "label=disable",
                "--device", "/dev/fuse",
                "--uidmap", "0:0:1", "--uidmap", "1:1:65536",
                "--gidmap", "0:0:1", "--gidmap", "1:1:65536",
                "--cap-add", "SYS_ADMIN", "--cap-add", "MKNOD",
            ])

    # Mount host nix daemon socket + store so nix builds work inside the jail.
    # NIX_REMOTE=daemon forces nix to use the host daemon (which has nixbld users)
    # instead of trying local store access (which fails on UID mapping/permissions).
    nix_socket = Path("/nix/var/nix/daemon-socket")
    nix_store = Path("/nix/store")
    if nix_socket.exists() and nix_store.exists():
        docker_cmd.extend([
            "-v", f"{nix_socket}:{nix_socket}",
            "-v", f"{nix_store}:{nix_store}:ro",
            "-e", "NIX_REMOTE=daemon",
        ])

    # Podman rootless uses pasta networking by default (no nftables needed).
    # Only pass --net explicitly for non-default modes like "host".
    # Inside a container, always use host networking (netavark can't create
    # network namespaces without NET_ADMIN).
    if runtime == "podman" and in_container:
        docker_cmd.append("--net=host")
    elif net_mode != "bridge" or runtime == "docker":
        docker_cmd.append(f"--net={net_mode}")
    
    # Pass git name/email from host for clean commits inside jail
    # (We don't mount ~/.gitconfig to avoid exposing credentials/tokens)
    try:
        git_name = subprocess.check_output(
            ["git", "config", "--get", "user.name"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        if git_name:
            docker_cmd.extend(["-e", f"YOLO_GIT_NAME={git_name}"])
    except Exception:
        pass
    
    try:
        git_email = subprocess.check_output(
            ["git", "config", "--get", "user.email"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        if git_email:
            docker_cmd.extend(["-e", f"YOLO_GIT_EMAIL={git_email}"])
    except Exception:
        pass

    # Propagate host global gitignore into the jail
    # (We don't mount ~/.gitconfig to avoid credential leaks, but gitignore is safe)
    try:
        excludes_file = subprocess.check_output(
            ["git", "config", "--global", "--get", "core.excludesFile"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        if excludes_file:
            excludes_path = Path(excludes_file).expanduser()
        else:
            excludes_path = Path.home() / ".config" / "git" / "ignore"
    except Exception:
        excludes_path = Path.home() / ".config" / "git" / "ignore"
    if excludes_path.is_file():
        docker_cmd.extend(["-v", f"{excludes_path}:/home/agent/.config/git/ignore:ro"])
        docker_cmd.extend(["-e", "YOLO_GLOBAL_GITIGNORE=/home/agent/.config/git/ignore"])
    
    docker_cmd.extend(publish_args)
    docker_cmd.extend(mount_args)

    # Shadow workspace .vscode/mcp.json so agents use only our jail MCP config
    vscode_mcp = workspace / ".vscode" / "mcp.json"
    if vscode_mcp.exists():
        docker_cmd.extend(["-v", "/dev/null:/workspace/.vscode/mcp.json:ro"])

    # Mount host mise at its original path so .venv symlinks created on host resolve inside jail.
    # When nested, pass the path as an env var so inner jails can re-mount it.
    host_mise = Path(os.environ.get("YOLO_OUTER_MISE_PATH") or os.environ.get("MISE_DATA_DIR", str(Path.home() / ".local/share/mise")))
    if host_mise.exists() and str(host_mise) != "/mise":
        docker_cmd.extend([
            "-v", f"{host_mise}:{host_mise}:ro",
            "-e", f"YOLO_OUTER_MISE_PATH={host_mise}",
        ])

    # Mount host user-level copilot/gemini skills so they're available in the jail
    host_gemini_skills = Path.home() / ".gemini" / "skills"
    host_dotfiles_skills = Path.home() / ".dotfiles" / "gemini" / "skills"
    
    if host_gemini_skills.exists() and host_gemini_skills.is_dir():
        docker_cmd.extend(["-v", f"{host_gemini_skills}:/ctx/host-gemini-skills:ro"])
        docker_cmd.extend(["-e", "YOLO_HOST_GEMINI_SKILLS=/ctx/host-gemini-skills"])
        
        if host_dotfiles_skills.exists() and host_dotfiles_skills.is_dir():
            docker_cmd.extend(["-v", f"{host_dotfiles_skills}:{host_dotfiles_skills}:ro"])

    # Generate per-workspace AGENTS.md (separate for Copilot and Gemini to
    # respect user-level ~/.copilot/AGENTS.md vs ~/.gemini/AGENTS.md)
    agents_path = generate_agents_md(cname, workspace, normalized_blocked, mount_descriptions, net_mode=net_mode, runtime=runtime)
    docker_cmd.extend(["-v", f"{agents_path / 'AGENTS-copilot.md'}:/home/agent/.copilot/AGENTS.md:ro"])
    docker_cmd.extend(["-v", f"{agents_path / 'AGENTS-gemini.md'}:/home/agent/.gemini/AGENTS.md:ro"])

    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    docker_cmd.append(JAIL_IMAGE)
    docker_cmd.append("yolo-entrypoint")

    # If mise.toml exists in workspace, trust it.
    # Then ensure all tools (global + local) are ready.
    setup_script = "YOLO_BYPASS_SHIMS=1 sh -c '(if [ -f mise.toml ]; then mise trust; fi) && mise install && mise upgrade && ~/.yolo-bootstrap.sh'"
    # After setup, activate mise so tool paths (copilot, gemini, etc.) are in PATH
    final_internal_cmd = f"{setup_script} >/dev/null 2>&1; eval \"$(mise hook-env -s bash)\" 2>/dev/null; {target_cmd}"
    
    docker_cmd.append(final_internal_cmd)

    write_container_tracking(cname, workspace)
    _tmux_rename_window("JAIL")

    # Use subprocess.run (not execvp) so atexit handlers fire for tmux cleanup
    result = subprocess.run(docker_cmd)
    sys.exit(result.returncode)


@app.command()
def ps():
    """List running YOLO jail containers."""
    runtime = _runtime()
    result = subprocess.run(
        [runtime, "ps", "--filter", "name=^yolo-", "--format", "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        typer.echo(result.stdout.strip())
        # Show workspace mappings, clean up stale tracking files
        for tracking_file in sorted(CONTAINER_DIR.iterdir()) if CONTAINER_DIR.exists() else []:
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

def main():
    """Entry point for the `yolo` console script.

    Handles tmux pane decoration and routes to the typer CLI.
    """
    import atexit

    restore = _tmux_setup_jail_pane()
    if restore:
        atexit.register(restore)

    app()


if __name__ == "__main__":
    main()
