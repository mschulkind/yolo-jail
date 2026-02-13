import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, List
import typer
try:
    import tomli as toml
except ImportError:
    import tomllib as toml  # type: ignore

app = typer.Typer()

JAIL_IMAGE = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"

def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)

def load_config() -> dict:
    config_path = Path.cwd() / "yolo-jail.toml"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as f:
            return toml.load(f)
    except Exception as e:
        typer.echo(f"Warning: Failed to parse yolo-jail.toml: {e}", err=True)
        return {}

@app.command()
def init():
    """Initialize a yolo-jail.toml configuration file in the current directory."""
    config_path = Path.cwd() / "yolo-jail.toml"
    if config_path.exists():
        typer.echo("yolo-jail.toml already exists.")
        return

    content = """[security]
# Tools to strictly block inside the jail
blocked_tools = ["curl", "wget", "grep", "find"]

[network]
# Networking mode: "bridge" (default, isolated IP) or "host" (shares host IP/ports)
# mode = "bridge" 
# Ports to publish (only for bridge mode)
# ports = ["8000:8000"]
"""
    with open(config_path, "w") as f:
        f.write(content)
    typer.echo("Created yolo-jail.toml")

@app.command()
def run(
    command: Optional[List[str]] = typer.Argument(None, help="Command to run inside the jail (default: bash)"),
    network: str = typer.Option("bridge", help="Docker network mode (bridge/host)"),
):
    """Run the YOLO jail in the current directory."""
    ensure_global_storage()
    config = load_config()
    
    # Determine Network Mode
    # CLI arg overrides config, config overrides default
    net_mode = network
    if config.get("network", {}).get("mode"):
        net_mode = config["network"]["mode"]
    
    # Determine Ports
    publish_args = []
    if net_mode == "bridge" and config.get("network", {}).get("ports"):
        for p in config["network"]["ports"]:
            publish_args.extend(["-p", p])

    # Construct Docker Command
    docker_cmd = [
        "docker", "run", "--rm", "-it",
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
        "-u", f"{os.getuid()}:{os.getgid()}",
        "--workdir", "/workspace",
        f"--net={net_mode}",
    ]
    
    docker_cmd.extend(publish_args)

    # Filter host environment variables to prevent leakage, but pass TERM
    # Justfile approach: --env-file <(env | grep -v ...)
    # In python, we explicitly don't pass --env-file, so docker starts clean.
    # We only pass what we explicitly set above.
    # EXCEPT: We probably want TERM for colors.
    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    docker_cmd.append(JAIL_IMAGE)
    docker_cmd.append("yolo-entrypoint")

    # Construct the internal shell command
    # This logic handles the auto-install of tools via mise
    
    # If command is provided, run it. If not, run bash.
    target_cmd = "bash"
    if command:
        target_cmd = " ".join(command) # This is a bit simplistic, might need better quoting if complex
        # Actually, yolo-entrypoint executes bash -c "$@", so we pass the string.
    
    # The setup script that runs before the user command
    setup_script = "[[ -f mise.toml ]] && (mise trust && YOLO_BYPASS_SHIMS=1 mise install && YOLO_BYPASS_SHIMS=1 mise upgrade)"
    
    # Combine them
    final_internal_cmd = f"{setup_script}; {target_cmd}"
    
    docker_cmd.append(final_internal_cmd)

    try:
        # We use os.execvp to replace the python process with docker
        # This keeps TTY handling correct
        os.execvp("docker", docker_cmd)
    except FileNotFoundError:
        typer.echo("Error: docker command not found.", err=True)
        sys.exit(1)

if __name__ == "__main__":
    app()
