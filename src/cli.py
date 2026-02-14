import os
import subprocess
import sys
import json
import shlex
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import typer
import pyjson5

app = typer.Typer()

JAIL_IMAGE = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"

from rich.console import Console
from rich.status import Status

console = Console()

def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)

def auto_load_image(repo_root: Path):
    """Cheaply check if the nix image needs to be reloaded into docker."""
    sentinel = repo_root / ".last-load"
    
    # 1. Build the image (cheap if no changes)
    with console.status("[bold blue]Checking jail image...", spinner="dots"):
        try:
            # Use a temporary result link
            subprocess.run(
                ["nix", "--extra-experimental-features", "nix-command flakes", "build", ".#dockerImage", "--out-link", ".run-result"],
                cwd=repo_root, check=True, capture_output=True
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

def load_config() -> Dict[str, Any]:
    # Only support JSONC
    jsonc_path = Path.cwd() / "yolo-jail.jsonc"
    if jsonc_path.exists():
        try:
            with open(jsonc_path, "r") as f:
                return pyjson5.load(f)
        except Exception as e:
            typer.echo(f"Warning: Failed to parse yolo-jail.jsonc: {e}", err=True)
            return {}
    
    return {}

@app.command()
def init():
    """Initialize a yolo-jail.jsonc configuration file in the current directory."""
    config_path = Path.cwd() / "yolo-jail.jsonc"
    if config_path.exists():
        typer.echo("yolo-jail.jsonc already exists.")
        return

    content = """{
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
    // \"bridge\" (default) or \"host\"
    "mode": "bridge",
    // Ports to publish in bridge mode [\"Host:Container\"]
    // \"ports\": [\"8000:8000\"]
  },
  // Extra host paths to mount read-only into the jail for context.
  // Each entry is a host path (mounted at /ctx/<basename>) or "host:container".
  // \"mounts\": [
  //   \"~/code/other-repo\",
  //   \"/home/matt/code/shared-lib:/ctx/shared-lib\"
  // ]
}
"""
    with open(config_path, "w") as f:
        f.write(content)
    typer.echo("Created yolo-jail.jsonc")

@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    ctx: typer.Context,
    network: str = typer.Option("bridge", help="Docker network mode (bridge/host)"),
):
    """Run the YOLO jail in the current directory."""
    # Find repo root to locate sentinel file
    repo_root = Path(__file__).parent.parent.resolve()
    auto_load_image(repo_root)
    
    ensure_global_storage()
    config = load_config()
    
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

    normalized_blocked = []
    
    for tool in raw_blocked:
        if isinstance(tool, str):
            normalized_blocked.append({"name": tool})
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
    docker_flags = ["--rm", "-i"]
    if sys.stdout.isatty():
        docker_flags.append("-t")

    docker_cmd = [
        "docker", "run", *docker_flags,
        "-v", f"{Path.cwd()}:/workspace",
        "-v", f"{GLOBAL_HOME}:/home/agent",
        "-v", f"{GLOBAL_MISE}:/mise",
        "--tmpfs", "/tmp",
        "--shm-size=2g",
        "-e", "HOME=/home/agent",
        "-e", "MISE_DATA_DIR=/mise",
        "-e", "MISE_TRUST=1",
        "-e", "MISE_YES=1",
        "-e", "COPILOT_ALLOW_ALL=true",
        "-e", "LD_LIBRARY_PATH=/lib:/usr/lib",
        "-e", "PATH=/home/agent/.npm-global/bin:/home/agent/go/bin:/mise/shims:/bin:/usr/bin",
        "-e", f"YOLO_BLOCK_CONFIG={blocked_config_json}",
        "-e", f"YOLO_HOST_DIR={Path.cwd()}",
        "-e", f"YOLO_MOUNTS={json.dumps(mount_descriptions)}",
        "-u", f"{os.getuid()}:{os.getgid()}",
        "--workdir", "/workspace",
        f"--net={net_mode}",
    ]
    
    docker_cmd.extend(publish_args)
    docker_cmd.extend(mount_args)

    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    docker_cmd.append(JAIL_IMAGE)
    docker_cmd.append("yolo-entrypoint")

    # Command construction
    full_command = ctx.args

    target_cmd = "bash"
    if full_command:
        # If calling gemini or copilot, inject --yolo
        if full_command[0] in ["gemini", "copilot"]:
            if "--yolo" not in full_command and "-y" not in full_command:
                full_command.insert(1, "--yolo")
        
        # Use shlex.join to properly quote arguments for the shell
        target_cmd = shlex.join(full_command)
    
    # If mise.toml exists in workspace, trust it. 
    # Then ensure all tools (global + local) are ready.
    setup_script = "(if [ -f mise.toml ]; then mise trust; fi) && mise install && mise upgrade && ~/.yolo-bootstrap.sh"
    final_internal_cmd = f"{setup_script} >/dev/null 2>&1; {target_cmd}"
    
    docker_cmd.append(final_internal_cmd)

    try:
        os.execvp("docker", docker_cmd)
    except FileNotFoundError:
        typer.echo("Error: docker command not found.", err=True)
        sys.exit(1)

if __name__ == "__main__":
    app()