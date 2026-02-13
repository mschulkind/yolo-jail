import os
import subprocess
import sys
import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import typer
import pyjson5

app = typer.Typer()

JAIL_IMAGE = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"

def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)

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
  }
}
"""
    with open(config_path, "w") as f:
        f.write(content)
    typer.echo("Created yolo-jail.jsonc")

@app.command()
def run(
    command: Optional[List[str]] = typer.Argument(None, help="Command to run inside the jail (default: bash)"),
    network: str = typer.Option("bridge", help="Docker network mode (bridge/host)"),
):
    """Run the YOLO jail in the current directory."""
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
        "-e", "HOME=/home/agent",
        "-e", "XDG_CONFIG_HOME=/home/agent/.config",
        "-e", "MISE_DATA_DIR=/mise",
        "-e", "MISE_CONFIG_DIR=/workspace",
        "-e", "MISE_TRUST=1",
        "-e", "MISE_YES=1",
        "-e", "LD_LIBRARY_PATH=/lib:/usr/lib",
        "-e", "PATH=/mise/shims:/bin:/usr/bin",
        "-e", f"YOLO_BLOCK_CONFIG={blocked_config_json}",
        "-u", f"{os.getuid()}:{os.getgid()}",
        "--workdir", "/workspace",
        f"--net={net_mode}",
    ]
    
    docker_cmd.extend(publish_args)

    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    docker_cmd.append(JAIL_IMAGE)
    docker_cmd.append("yolo-entrypoint")

    # Command construction
    target_cmd = "bash"
    if command:
        target_cmd = " ".join(command)
    
    setup_script = "[[ -f mise.toml ]] && (mise trust && YOLO_BYPASS_SHIMS=1 mise install && YOLO_BYPASS_SHIMS=1 mise upgrade)"
    final_internal_cmd = f"{setup_script}; {target_cmd}"
    
    docker_cmd.append(final_internal_cmd)

    try:
        os.execvp("docker", docker_cmd)
    except FileNotFoundError:
        typer.echo("Error: docker command not found.", err=True)
        sys.exit(1)

if __name__ == "__main__":
    app()