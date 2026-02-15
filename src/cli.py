import os
import subprocess
import sys
import json
import shlex
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import typer
import pyjson5

app = typer.Typer()

JAIL_IMAGE = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"
CONTAINER_DIR = GLOBAL_STORAGE / "containers"
USER_CONFIG_PATH = Path.home() / ".config" / "yolo-jail" / "config.jsonc"

from rich.console import Console
from rich.status import Status

console = Console()

def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)
    CONTAINER_DIR.mkdir(parents=True, exist_ok=True)


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic container name from workspace path."""
    h = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:12]
    return f"yolo-{h}"


def find_running_container(name: str) -> Optional[str]:
    """Return container ID if a container with this name is running, else None."""
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^/{name}$"],
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

def auto_load_image(repo_root: Path, extra_packages: List[str] = None):
    """Cheaply check if the nix image needs to be reloaded into docker."""
    sentinel = repo_root / ".last-load"
    
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
                    ["docker", "load"],
                    stdin=image_file,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                
                with console.status("[bold cyan]Loading into Docker...", spinner="bouncingBar") as status:
                    if process.stdout:
                        for line in iter(process.stdout.readline, ""):
                            clean_line = line.strip()
                            if clean_line:
                                # Show the last line of docker load (e.g. "Loaded image: ...")
                                status.update(f"[bold cyan]Loading: [dim]{clean_line}[/dim]")
                                last_line = clean_line
                
                process.wait()
                if process.returncode != 0:
                    console.print("[bold red]Error loading docker image.[/bold red]")
                else:
                    console.print(f"[bold green]Successfully {last_line.lower() if 'last_line' in locals() else 'loaded image'}[/bold green]")
                    sentinel.write_text(str(current_path))
        except Exception as e:
            console.print(f"[bold red]Error loading docker image: {e}[/bold red]")
    
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
  // Extra nix packages to include in the jail image.
  // Names must match nixpkgs attribute names (search at https://search.nixos.org/packages).
  // The image rebuilds only when this list changes.
  // "packages": ["postgresql", "redis", "awscli2"],

  "security": {
    // Tools to block. Can be a simple string or an object with custom messages.
    "blocked_tools": [
      "curl", 
      "wget",
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
    network: str = typer.Option("bridge", help="Docker network mode (bridge/host)"),
    new: bool = typer.Option(False, "--new", help="Force a new container even if one already exists for this workspace"),
):
    """Run the YOLO jail in the current directory."""
    # Find repo root to locate sentinel file
    repo_root = Path(__file__).parent.parent.resolve()
    workspace = Path.cwd()
    
    ensure_global_storage()
    config = load_config()
    
    # Build image with any extra packages from config
    extra_packages = config.get("packages", [])
    auto_load_image(repo_root, extra_packages=extra_packages or None)
    
    # Command construction (needed for both exec and run paths)
    full_command = list(ctx.args)

    target_cmd = "bash"
    if full_command:
        # If calling gemini or copilot, inject --yolo
        if full_command[0] in ["gemini", "copilot"]:
            if "--yolo" not in full_command and "-y" not in full_command:
                full_command.insert(1, "--yolo")
        target_cmd = shlex.join(full_command)

    # Check for existing container for this workspace
    cname = container_name_for_workspace(workspace)
    existing_cid = None if new else find_running_container(cname)

    if existing_cid:
        # Exec into the existing container
        console.print(f"[bold cyan]Attaching to existing jail [dim]({cname})[/dim]...[/bold cyan]")
        exec_flags = ["-i"]
        if sys.stdout.isatty():
            exec_flags.append("-t")
        docker_cmd = [
            "docker", "exec", *exec_flags,
            cname,
            "yolo-entrypoint", target_cmd,
        ]
        try:
            os.execvp("docker", docker_cmd)
        except FileNotFoundError:
            typer.echo("Error: docker command not found.", err=True)
            sys.exit(1)
        return

    # --- New container path ---

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

    docker_cmd = [
        "docker", "run", *docker_flags,
        "-v", f"{workspace}:/workspace",
        "-v", f"{GLOBAL_HOME}:/home/agent",
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
        "-e", f"YOLO_MOUNTS={json.dumps(mount_descriptions)}",
        "-u", f"{os.getuid()}:{os.getgid()}",
        "--workdir", "/workspace",
        f"--net={net_mode}",
    ]
    
    docker_cmd.extend(publish_args)
    docker_cmd.extend(mount_args)

    # Shadow workspace .vscode/mcp.json so agents use only our jail MCP config
    vscode_mcp = workspace / ".vscode" / "mcp.json"
    if vscode_mcp.exists():
        docker_cmd.extend(["-v", "/dev/null:/workspace/.vscode/mcp.json:ro"])

    # Mount host mise at its original path so .venv symlinks created on host resolve inside jail
    host_mise = Path(os.environ.get("MISE_DATA_DIR", str(Path.home() / ".local/share/mise")))
    if host_mise.exists():
        docker_cmd.extend(["-v", f"{host_mise}:{host_mise}:ro"])

    # Mount host user-level copilot/gemini skills so they're available in the jail
    host_gemini_skills = Path.home() / ".gemini" / "skills"
    host_dotfiles_skills = Path.home() / ".dotfiles" / "gemini" / "skills"
    
    if host_gemini_skills.exists() and host_gemini_skills.is_dir():
        docker_cmd.extend(["-v", f"{host_gemini_skills}:/ctx/host-gemini-skills:ro"])
        docker_cmd.extend(["-e", "YOLO_HOST_GEMINI_SKILLS=/ctx/host-gemini-skills"])
        
        if host_dotfiles_skills.exists() and host_dotfiles_skills.is_dir():
            docker_cmd.extend(["-v", f"{host_dotfiles_skills}:{host_dotfiles_skills}:ro"])

    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    docker_cmd.append(JAIL_IMAGE)
    docker_cmd.append("yolo-entrypoint")

    # If mise.toml exists in workspace, trust it.
    # Then ensure all tools (global + local) are ready.
    setup_script = "YOLO_BYPASS_SHIMS=1 sh -c '(if [ -f mise.toml ]; then mise trust; fi) && mise install && mise upgrade && ~/.yolo-bootstrap.sh'"
    final_internal_cmd = f"{setup_script} >/dev/null 2>&1; {target_cmd}"
    
    docker_cmd.append(final_internal_cmd)

    # Write tracking file before exec (since execvp replaces our process)
    write_container_tracking(cname, workspace)

    try:
        os.execvp("docker", docker_cmd)
    except FileNotFoundError:
        typer.echo("Error: docker command not found.", err=True)
        sys.exit(1)


@app.command()
def ps():
    """List running YOLO jail containers."""
    result = subprocess.run(
        ["docker", "ps", "--filter", "name=^yolo-", "--format", "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}"],
        capture_output=True, text=True,
    )
    if result.stdout.strip():
        typer.echo(result.stdout.strip())
        # Show workspace mappings from tracking files
        for tracking_file in sorted(CONTAINER_DIR.iterdir()) if CONTAINER_DIR.exists() else []:
            workspace_path = tracking_file.read_text().strip()
            typer.echo(f"  {tracking_file.name} → {workspace_path}")
    else:
        typer.echo("No running jails.")

if __name__ == "__main__":
    app()
