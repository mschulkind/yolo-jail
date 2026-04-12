import difflib
import fcntl
import os
import platform
import re
import socket
import struct
import subprocess
import sys
import json
import shlex
import shutil
import hashlib
import time
import tempfile
import threading
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import typer
import pyjson5
from rich.console import Console

IS_LINUX = sys.platform == "linux"
IS_MACOS = sys.platform == "darwin"

app = typer.Typer(
    invoke_without_command=True,
    rich_markup_mode="rich",
    no_args_is_help=False,
)


def _version_callback(value: bool):
    if value:
        v = _get_yolo_version()
        typer.echo(f"yolo-jail {v}")
        raise typer.Exit()


def _git_describe_version() -> "str | None":
    """Derive a version string from ``git describe --tags --dirty --always``.

    Returns a cleaned version such as ``0.1.0``, ``0.1.0+3.gabcdef1``, or
    ``0.1.0+3.gabcdef1.dirty``.  Returns *None* when git is unavailable or
    the command fails (e.g. not a git checkout).
    """
    # First check env var (set by host CLI when launching a jail)
    raw = os.environ.get("YOLO_VERSION")
    if raw:
        return raw
    try:
        repo_root = _resolve_repo_root()
    except Exception:
        repo_root = Path(__file__).resolve().parent.parent

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=repo_root,
        )
        if result.returncode == 0:
            raw = result.stdout.strip()
    except Exception:
        pass

    # Fall back to setuptools-scm baked version (in installed wheels)
    if raw is None:
        try:
            from src._version import version as scm_version

            raw = scm_version
        except Exception:
            pass

    # Fall back to package metadata
    if raw is None:
        try:
            from importlib.metadata import version as pkg_version

            raw = pkg_version("yolo-jail")
        except Exception:
            return None

    if raw is None:
        return None

    # Strip leading 'v' (e.g. v0.1.0 -> 0.1.0)
    if raw.startswith("v"):
        raw = raw[1:]

    # git format: 0.1.0-3-gabcdef1-dirty  ->  0.1.0+3.gabcdef1.dirty
    # Exactly on tag: 0.1.0              ->  0.1.0
    # Dirty on tag:   0.1.0-dirty        ->  0.1.0.dirty
    parts = raw.split("-")

    # Find the boundary between the version and the extra components.
    # Semantic versions contain hyphens too (e.g. 1.0.0-rc.1), so we look
    # for the first part that is purely numeric (commit count) following at
    # least one version-like segment.
    # Strategy: walk from the end, collecting known suffixes.
    dirty = False
    if parts[-1] == "dirty":
        dirty = True
        parts = parts[:-1]

    # Check if the last part is a git abbreviated hash (g<hex>)
    commit_hash = None
    commit_count = None
    if len(parts) >= 2 and parts[-1].startswith("g") and parts[-2].isdigit():
        commit_hash = parts[-1]
        commit_count = parts[-2]
        parts = parts[:-2]

    base_version = "-".join(parts)

    suffix_parts: list[str] = []
    if commit_count is not None and commit_hash is not None:
        suffix_parts.append(commit_count)
        suffix_parts.append(commit_hash)
    if dirty:
        suffix_parts.append("dirty")

    if suffix_parts:
        # Use '+' to separate base version from build metadata, '.' between
        # metadata components.
        return f"{base_version}+{'.'.join(suffix_parts)}"
    return base_version


@app.callback()
def _default(
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
    version: bool = typer.Option(
        False,
        "--version",
        help="Show version and exit",
        callback=_version_callback,
        is_eager=True,
    ),
):
    """[bold]YOLO Jail[/bold] — Secure container environment for AI agents.

    Runs AI agents (Copilot, Gemini CLI, Claude Code) in isolated Docker/Podman containers
    with no access to host credentials (~/.ssh, ~/.gitconfig, cloud tokens).
    Tool state persists across restarts.

    [bold cyan]Quick Start[/bold cyan]

        yolo                      Interactive jail shell
        yolo -- copilot           Run Copilot in jail (--yolo auto-injected)
        yolo -- gemini            Run Gemini in jail (--yolo auto-injected)
        yolo -- claude            Run Claude Code in jail (YOLO mode via settings.json)
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
        Resources:  [bold]yolo-cglimit[/bold] enforces CPU/memory/PID limits on
                    sub-processes via cgroup v2. See [bold]yolo config-ref[/bold].

        NOT shared: ~/.ssh, ~/.gitconfig, cloud credentials, host PATH.
        Blocked:    grep → rg, find → fd (configurable). Set YOLO_BYPASS_SHIMS=1
                    in scripts that need the originals.

    [bold cyan]Configuration[/bold cyan]

    Place [bold]yolo-jail.jsonc[/bold] in your project root (JSON with comments):

        {
          "runtime": "podman",              // or "docker" or "container" (Apple)
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

        YOLO_RUNTIME          Override runtime (podman/docker/container)
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
        ctx.invoke(run, ctx=ctx, network=network, new=new, profile=profile)


JAIL_IMAGE = "localhost/yolo-jail:latest"
# Apple Container CLI doesn't recognize the localhost/ prefix
JAIL_IMAGE_SHORT = "yolo-jail:latest"
GLOBAL_STORAGE = Path.home() / ".local/share/yolo-jail"
GLOBAL_HOME = GLOBAL_STORAGE / "home"
GLOBAL_MISE = GLOBAL_STORAGE / "mise"
GLOBAL_CACHE = GLOBAL_STORAGE / "cache"
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
      2. Source checkout detection (Path(__file__) → parent → parent → flake.nix exists)
      3. Installed package detection (flake.nix bundled inside the src/ package)
         Stages a build directory with symlinks so nix sees the expected layout.
      4. User config repo_path field (~/.config/yolo-jail/config.jsonc)
      5. Error with helpful message
    """
    # 1. Env var (used inside jails, CI, etc.)
    env_val = os.environ.get("YOLO_REPO_ROOT")
    if env_val:
        return Path(env_val).resolve()

    # 2. Running from source checkout (dev mode)
    source_root = Path(__file__).parent.parent
    if (source_root / "flake.nix").exists():
        return source_root.resolve()

    # 3. Installed package — flake.nix bundled as package data in src/
    pkg_dir = Path(__file__).parent
    if (pkg_dir / "flake.nix").exists():
        build_root = GLOBAL_STORAGE / "nix-build-root"
        build_root.mkdir(parents=True, exist_ok=True)
        # Copy flake files (not symlinks — nix resolves symlinks to absolute
        # paths that break inside the store).
        import shutil

        # Use a temp dir + atomic rename to avoid races when multiple
        # jails start concurrently and all try to populate nix-build-root.
        import tempfile

        tmp_root = Path(tempfile.mkdtemp(dir=GLOBAL_STORAGE, prefix="nix-build-tmp-"))
        try:
            for fname in ("flake.nix", "flake.lock"):
                shutil.copy2(pkg_dir / fname, tmp_root / fname)
            shutil.copytree(pkg_dir, tmp_root / "src")
            # Atomic swap: rename over the target so concurrent readers
            # either see the old version or the new one, never a half-written state.
            target_tmp = build_root.with_name(build_root.name + ".old")
            try:
                build_root.rename(target_tmp)
            except FileNotFoundError:
                target_tmp = None
            tmp_root.rename(build_root)
            if target_tmp and target_tmp.exists():
                shutil.rmtree(target_tmp, ignore_errors=True)
        except BaseException:
            shutil.rmtree(tmp_root, ignore_errors=True)
            raise
        return build_root.resolve()

    # 4. User config
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
        '  { "repo_path": "~/code/yolo-jail" }'
    )
    raise typer.Exit(1)


def ensure_global_storage():
    GLOBAL_HOME.mkdir(parents=True, exist_ok=True)
    GLOBAL_MISE.mkdir(parents=True, exist_ok=True)
    GLOBAL_CACHE.mkdir(parents=True, exist_ok=True)
    CONTAINER_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    # Pre-create directories and files inside GLOBAL_HOME that will be mountpoints
    # for bind mounts.  GLOBAL_HOME is mounted :ro, so the container runtime cannot
    # create these on the fly — they must already exist in the base filesystem.
    for subdir in [
        ".copilot",
        ".gemini",
        ".claude",
        Path(".config") / "git",
        ".npm-global",
        ".local",
        "go",
        ".yolo-shims",
        ".config",
        ".cache",
        ".ssh",
    ]:
        (GLOBAL_HOME / subdir).mkdir(parents=True, exist_ok=True)
    # File mountpoints — these must exist as files (not dirs) for bind mounts.
    # Only create if missing — existing files from prior runs may have restrictive
    # permissions from container UID mapping; we just need them to exist.
    for fname in [
        ".bash_history",
        ".yolo-bootstrap.sh",
        ".yolo-venv-precreate.sh",
        ".yolo-perf.log",
        ".yolo-socat.log",
        ".yolo-entrypoint.lock",
    ]:
        p = GLOBAL_HOME / fname
        if not p.exists():
            p.touch()
    # Files in the :ro /home/agent/ that need atomic writes (lock-file-then-rename)
    # must be symlinks into writable overlay dirs.  Without this, tools like git,
    # Claude Code, and bash fail with EROFS when trying to update these files.
    _ensure_symlink(
        GLOBAL_HOME / ".claude.json",
        Path(".claude") / "claude.json",
    )
    # ~/.gitconfig must also be a symlink — git config uses lock-file-then-rename
    # which fails when the parent dir is :ro.  Point to .config/git/config (the
    # XDG standard location, already inside the writable .config/ overlay).
    _ensure_symlink(
        GLOBAL_HOME / ".gitconfig",
        Path(".config") / "git" / "config",
    )
    # ~/.bashrc — bash doesn't do atomic writes, but some tools that modify it
    # (e.g. mise activate) might.  Symlink into .config/ for safety.
    _ensure_symlink(
        GLOBAL_HOME / ".bashrc",
        Path(".config") / "bashrc",
    )


def _ensure_symlink(link: Path, target: Path):
    """Ensure link is a relative symlink to target, migrating regular files."""
    if link.is_symlink():
        if Path(os.readlink(str(link))) != target:
            link.unlink()
            link.symlink_to(target)
    elif link.exists():
        # Migrate data from old regular file to new target location
        real = link.parent / target
        if not real.exists():
            real.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(link, real)
            except OSError:
                pass  # unreadable (bad perms from prior container run) — skip data
        try:
            link.unlink()
            link.symlink_to(target)
        except OSError:
            pass  # can't replace — leave as-is
    else:
        link.symlink_to(target)


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


def _linux_multilib() -> str:
    """Return the Linux multilib directory name for the current architecture.

    The container is always Linux; the arch matches the host (native, not emulated).
    """
    machine = platform.machine()
    _MAP = {
        "x86_64": "x86_64-linux-gnu",
        "aarch64": "aarch64-linux-gnu",
        "arm64": "aarch64-linux-gnu",  # macOS reports arm64
    }
    return _MAP.get(machine, f"{machine}-linux-gnu")


def _is_apple_container(path: str) -> bool:
    """Return True if the binary at *path* is Apple's container CLI."""
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        )
        out = result.stdout + result.stderr
        # Match "Apple" or the distinctive "container CLI" version banner
        return "Apple" in out or "container CLI version" in out
    except Exception:
        return False


def _runtime_is_connectable(rt: str) -> bool:
    """Check if a container runtime daemon is reachable (not just the CLI)."""
    if rt == "container":
        # Apple Container: check system status
        try:
            result = subprocess.run(
                ["container", "system", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0 and "running" in result.stdout.lower()
        except Exception:
            return False
    try:
        result = subprocess.run(
            [rt, "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _runtime(config: Dict[str, Any] = None) -> str:
    """Return container runtime: 'podman', 'docker', or 'container' (Apple).

    Auto-detection priority:
      macOS: container → podman → docker  (native Apple Container preferred)
      Linux: podman → docker              (container CLI is macOS-only)

    Only returns runtimes whose daemon is actually reachable.
    """
    env = os.environ.get("YOLO_RUNTIME")
    if env and env in ("podman", "docker", "container"):
        return env
    if config:
        cfg = config.get("runtime")
        if cfg and cfg in ("podman", "docker", "container"):
            return cfg
    # Platform-aware auto-detection
    if IS_MACOS:
        candidates = ("container", "podman", "docker")
    else:
        candidates = ("podman", "docker")
    for rt in candidates:
        path = shutil.which(rt)
        if path:
            if rt == "container" and not _is_apple_container(path):
                continue
            if not _runtime_is_connectable(rt):
                continue
            return rt
    console.print(
        "[bold red]No container runtime found. Install podman, docker, or Apple's container CLI.[/bold red]"
    )
    sys.exit(1)


def container_name_for_workspace(workspace: Path) -> str:
    """Deterministic container name from workspace path.

    Uses the directory name for readability (e.g. yolo-tillr) with a short
    hash suffix to handle collisions (e.g. two dirs both named 'app').
    """
    name = workspace.resolve().name
    # Sanitize for container naming: lowercase, alphanumeric + hyphens
    safe = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]
    if not safe:
        safe = "jail"
    h = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:8]
    return f"yolo-{safe}-{h}"


def find_running_container(name: str, runtime: str = "docker") -> Optional[str]:
    """Return container ID if a container with this name is running, else None."""
    try:
        if runtime == "container":
            # Apple Container CLI: 'ls' shows running containers by default.
            # --filter is not supported; scan the table output instead.
            result = subprocess.run(
                ["container", "ls"],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.strip().splitlines()[1:]:  # skip header
                parts = line.split()
                if parts and parts[0] == name:
                    return name
            return None
        else:
            result = subprocess.run(
                [runtime, "ps", "-q", "--filter", f"name=^/{name}$"],
                capture_output=True,
                text=True,
            )
    except FileNotFoundError:
        return None
    cid = result.stdout.strip()
    return cid if cid else None


def find_existing_container(name: str, runtime: str = "docker") -> Optional[str]:
    """Return container ID if a container with this name exists (running OR stopped)."""
    try:
        if runtime == "container":
            # Apple Container CLI: 'ls' only shows running by default;
            # use --all to include stopped containers.
            # --filter is not supported; scan the table output instead.
            result = subprocess.run(
                ["container", "ls", "--all"],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if parts and parts[0] == name:
                    return name
            return None
        result = subprocess.run(
            [runtime, "ps", "-a", "-q", "--filter", f"name=^/{name}$"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    cid = result.stdout.strip()
    return cid if cid else None


def _remove_stale_container(name: str, runtime: str = "docker") -> bool:
    """Remove a stopped container. Returns True if removal succeeded."""
    try:
        if runtime == "container":
            # Apple Container CLI: use 'delete' (aliased as 'rm') with --force
            result = subprocess.run(
                ["container", "rm", "--force", name],
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                [runtime, "rm", name],
                capture_output=True,
                text=True,
            )
        if result.returncode == 0:
            cleanup_container_tracking(name)
            return True
        return False
    except FileNotFoundError:
        return False


def _print_startup_banner(
    version: str, runtime: str, cname: str, res_parts: "list[str] | None" = None
):
    """Print startup info to stderr for debugging and log sharing."""
    host_platform = f"{sys.platform}/{platform.machine()}"
    parts = [f"yolo-jail {version}", host_platform, runtime, cname]
    print(" | ".join(parts), file=sys.stderr)
    if res_parts:
        print(f"Resource limits: {', '.join(res_parts)}", file=sys.stderr)


def _get_yolo_version() -> str:
    """Return the yolo-jail version string."""
    v = _git_describe_version()
    if v is None:
        from importlib.metadata import version as pkg_version

        try:
            v = pkg_version("yolo-jail")
        except Exception:
            v = "unknown"
    return v


def _image_load_cmd(runtime: str, tar_path: str) -> list[str]:
    """Return the command to load a container image from a tar archive."""
    if runtime == "container":
        return ["container", "image", "load", "-i", tar_path]
    return [runtime, "load", "-i", tar_path]


def _load_image_for_apple_container(tar_path: str, console, status=None) -> bool:
    """Load a Nix-built Docker-format tar into Apple Container CLI.

    Apple Container only accepts OCI-layout tars, but Nix's dockerTools
    produces Docker V2 format.  We convert using (in priority order):
      1. skopeo (no daemon needed — works standalone)
      2. podman save --format oci-archive (needs Podman Machine)
      3. docker save (needs Docker daemon)
    """
    skopeo = shutil.which("skopeo")
    if skopeo:
        return _convert_via_skopeo(tar_path, console, status)

    # Fall back to podman or docker daemon-based conversion
    for rt_name, rt_bin in [
        ("Podman", shutil.which("podman")),
        ("Docker", shutil.which("docker")),
    ]:
        if not rt_bin:
            continue
        return _convert_via_daemon(rt_name.lower(), tar_path, console, status)

    console.print(
        "[bold red]Cannot convert Nix image to OCI format for Apple Container.[/bold red]"
    )
    console.print(
        "[dim]Install one of: skopeo (recommended, no daemon needed), podman, or docker.[/dim]"
    )
    return False


def _convert_via_skopeo(tar_path: str, console, status=None) -> bool:
    """Convert Docker V2 tar → OCI tar via skopeo (no daemon needed)."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="yolo-oci-") as oci_dir:
        if status:
            status.update("[bold cyan]Converting to OCI format via skopeo...")
        copy_result = subprocess.run(
            [
                "skopeo",
                "copy",
                f"docker-archive:{tar_path}",
                f"oci:{oci_dir}:{JAIL_IMAGE_SHORT}",
            ],
            capture_output=True,
        )
        if copy_result.returncode != 0:
            console.print("[bold red]skopeo conversion to OCI failed.[/bold red]")
            stderr = copy_result.stderr.decode().strip()
            if stderr:
                console.print(f"  [dim]{stderr}[/dim]")
            return False

        # Tar up the OCI directory for Apple Container
        oci_tar = tar_path + ".oci.tar"
        if status:
            status.update("[bold cyan]Loading OCI image into Apple Container...")
        tar_result = subprocess.run(
            ["tar", "cf", oci_tar, "-C", oci_dir, "."],
            capture_output=True,
        )
        if tar_result.returncode != 0:
            console.print("[bold red]Failed to create OCI tar.[/bold red]")
            return False

        apple_result = subprocess.run(
            ["container", "image", "load", "-i", oci_tar],
            capture_output=True,
        )
        Path(oci_tar).unlink(missing_ok=True)

        if apple_result.returncode != 0:
            console.print(
                "[bold red]Failed to load OCI image into Apple Container.[/bold red]"
            )
            stderr = apple_result.stderr.decode().strip()
            if stderr:
                console.print(f"  [dim]{stderr}[/dim]")
            return False

    return True


def _convert_via_daemon(daemon: str, tar_path: str, console, status=None) -> bool:
    """Convert Docker V2 tar → OCI tar via docker/podman daemon save."""
    if status:
        status.update(f"[bold cyan]Loading image into {daemon} (for OCI conversion)...")
    load_result = subprocess.run(
        [daemon, "load", "-i", tar_path],
        capture_output=True,
    )
    if load_result.returncode != 0:
        console.print(
            f"[bold red]Failed to load image into {daemon} for conversion.[/bold red]"
        )
        stderr = load_result.stderr.decode().strip()
        if stderr:
            console.print(f"  [dim]{stderr}[/dim]")
        return False

    oci_tar = tar_path + ".oci.tar"
    if status:
        status.update(f"[bold cyan]Converting to OCI format via {daemon} save...")
    if daemon == "podman":
        save_cmd = [
            daemon,
            "save",
            "--format",
            "oci-archive",
            "-o",
            oci_tar,
            JAIL_IMAGE,
        ]
    else:
        # docker save produces Docker V2 tar, not OCI. Use skopeo to convert
        # if available, otherwise fall back to docker save (may fail on Apple
        # Container which strictly requires OCI format).
        if shutil.which("skopeo"):
            save_cmd = [
                "skopeo",
                "copy",
                f"docker-daemon:{JAIL_IMAGE}",
                f"oci-archive:{oci_tar}",
            ]
        else:
            save_cmd = [daemon, "save", "-o", oci_tar, JAIL_IMAGE]
    save_result = subprocess.run(save_cmd, capture_output=True)
    if save_result.returncode != 0:
        console.print(f"[bold red]Failed to export OCI image from {daemon}.[/bold red]")
        return False

    if status:
        status.update("[bold cyan]Loading OCI image into Apple Container...")
    apple_result = subprocess.run(
        ["container", "image", "load", "-i", oci_tar],
        capture_output=True,
    )
    Path(oci_tar).unlink(missing_ok=True)

    if apple_result.returncode != 0:
        console.print(
            "[bold red]Failed to load OCI image into Apple Container.[/bold red]"
        )
        stderr = apple_result.stderr.decode().strip()
        if stderr:
            console.print(f"  [dim]{stderr}[/dim]")
        return False

    return True


def _image_inspect_cmd(runtime: str, image: str) -> list[str]:
    """Return the command to inspect a container image."""
    return [runtime, "image", "inspect", image]


def _jail_image(runtime: str) -> str:
    """Return the jail image name appropriate for the given runtime."""
    if runtime == "container":
        return JAIL_IMAGE_SHORT
    return JAIL_IMAGE


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


# ---------------------------------------------------------------------------
# Host-side cgroup delegate daemon
# ---------------------------------------------------------------------------
# Instead of giving the container CAP_SYS_ADMIN to remount /sys/fs/cgroup rw,
# we run a thin host-side daemon that performs cgroup operations on behalf of
# the container.  The daemon listens on a Unix socket that is bind-mounted
# into the jail.  The container-side `yolo-cglimit` client sends JSON
# requests; the daemon validates them and performs the cgroup writes on the
# host filesystem where cgroup v2 is writable.
#
# Security model:
#   - All cgroup operations are confined to the container's cgroup subtree
#   - Cgroup names are strictly validated (alphanumeric + dash/underscore)
#   - Limit values are range-checked
#   - PID identity comes from SO_PEERCRED (kernel-attested, unforgeable)
#   - Every operation is logged to a file for auditability
# ---------------------------------------------------------------------------

CGD_SOCKET_NAME = "cgroup.sock"


def _resolve_container_cgroup(cname: str, runtime: str) -> Optional[Path]:
    """Discover the host-side cgroup path for a running container.

    Returns the absolute Path to the container's cgroup directory on the host
    cgroup v2 filesystem, or None if it cannot be determined.

    Always returns None on macOS — cgroups are a Linux kernel feature.
    """
    if IS_MACOS:
        return None
    try:
        if runtime == "podman":
            # podman inspect returns the cgroup path (relative to cgroup root)
            result = subprocess.run(
                ["podman", "inspect", "--format", "{{.State.CgroupPath}}", cname],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                cg_path = result.stdout.strip()
                # Podman with systemd cgroup manager returns paths like
                # "user.slice/user-1000.slice/..." — these are already absolute
                # within /sys/fs/cgroup.
                candidate = Path("/sys/fs/cgroup") / cg_path
                if candidate.exists():
                    return candidate
                # Some podman versions return the scope name only
                # Try to find it via the container's init PID
        # Fallback for both Docker and Podman: use init PID's /proc cgroup
        fmt = "{{.State.Pid}}" if runtime == "docker" else "{{.State.Pid}}"
        result = subprocess.run(
            [runtime, "inspect", "--format", fmt, cname],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        pid = int(result.stdout.strip())
        if pid <= 0:
            return None
        # Read /proc/<pid>/cgroup — format: "0::/path/to/cgroup"
        proc_cgroup = Path(f"/proc/{pid}/cgroup")
        if not proc_cgroup.exists():
            return None
        for line in proc_cgroup.read_text().splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                cg_rel = parts[2].lstrip("/")
                candidate = Path("/sys/fs/cgroup") / cg_rel
                if candidate.exists():
                    return candidate
    except Exception:
        pass
    return None


def _validate_cgroup_name(name: str) -> bool:
    """Validate that a cgroup name is safe (no path traversal)."""
    return (
        bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", name)) and ".." not in name
    )


def _parse_memory_value(val: str) -> Optional[int]:
    """Parse a human-readable memory value to bytes.  Returns None on invalid input."""
    val = val.strip().lower()
    try:
        if val.endswith("g"):
            return int(float(val[:-1]) * 1073741824)
        if val.endswith("m"):
            return int(float(val[:-1]) * 1048576)
        if val.endswith("k"):
            return int(float(val[:-1]) * 1024)
        return int(val)
    except (ValueError, OverflowError):
        return None


def _cgroup_delegate_handler(
    conn: socket.socket,
    container_cgroup: Path,
    log_file,
):
    """Handle a single cgroup delegate request from the container.

    Protocol: single-line JSON request, single-line JSON response.
    """
    try:
        data = b""
        while b"\n" not in data and len(data) < 4096:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if not data:
            return

        request = json.loads(data.decode("utf-8", errors="replace"))
        op = request.get("op", "")

        # Get the host-PID of the caller via SO_PEERCRED (Linux) or LOCAL_PEERPID (macOS)
        try:
            if IS_LINUX:
                cred = conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                peer_pid, peer_uid, peer_gid = struct.unpack("3i", cred)
            elif IS_MACOS:
                # macOS: LOCAL_PEERPID (0x002) returns the peer PID
                LOCAL_PEERPID = 0x002
                cred = conn.getsockopt(0, LOCAL_PEERPID, struct.calcsize("i"))
                peer_pid = struct.unpack("i", cred)[0]
            else:
                peer_pid = 0
        except (OSError, struct.error, AttributeError):
            peer_pid = 0

        # Log every request for auditability
        log_line = f"op={op} peer_pid={peer_pid} request={json.dumps(request)}"
        print(log_line, file=log_file, flush=True)

        if op == "status":
            # Check if delegation is available
            agent_cg = container_cgroup / "agent"
            controllers = ""
            if agent_cg.exists():
                try:
                    controllers = (agent_cg / "cgroup.controllers").read_text().strip()
                except OSError:
                    pass
            response = {
                "ok": True,
                "delegated": agent_cg.exists(),
                "controllers": controllers,
                "cgroup": str(container_cgroup),
            }

        elif op == "create_and_join":
            name = request.get("name", "")
            if not _validate_cgroup_name(name):
                response = {"ok": False, "error": f"Invalid cgroup name: {name!r}"}
            elif peer_pid <= 0:
                response = {"ok": False, "error": "Could not determine caller PID"}
            else:
                response = _cgd_create_and_join(
                    container_cgroup, name, request, peer_pid, log_file
                )

        elif op == "destroy":
            name = request.get("name", "")
            if not _validate_cgroup_name(name):
                response = {"ok": False, "error": f"Invalid cgroup name: {name!r}"}
            else:
                response = _cgd_destroy(container_cgroup, name, log_file)

        else:
            response = {"ok": False, "error": f"Unknown operation: {op!r}"}

        conn.sendall((json.dumps(response) + "\n").encode())
        print(f"  response={json.dumps(response)}", file=log_file, flush=True)

    except Exception as exc:
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
        except Exception:
            pass
    finally:
        conn.close()


def _cgd_ensure_agent_cgroup(container_cgroup: Path, log_file) -> Optional[Path]:
    """Ensure the agent cgroup subtree exists with controllers enabled.

    Returns the path to the agent cgroup, or None on failure.
    """
    agent_cg = container_cgroup / "agent"
    init_cg = container_cgroup / "init"

    if agent_cg.exists():
        return agent_cg

    try:
        init_cg.mkdir(exist_ok=True)
        agent_cg.mkdir(exist_ok=True)
    except OSError as e:
        print(f"  ERROR creating cgroup dirs: {e}", file=log_file, flush=True)
        return None

    # Move all existing processes to 'init' (cgroup v2 no-internal-process constraint)
    try:
        procs = (container_cgroup / "cgroup.procs").read_text().strip().split()
        for pid in procs:
            try:
                (init_cg / "cgroup.procs").write_text(pid)
            except OSError:
                pass  # Process may have exited or be a kthread
    except OSError:
        pass

    # Enable controllers on container root → agent subtree
    for cg in [container_cgroup, agent_cg]:
        try:
            available = (cg / "cgroup.controllers").read_text().strip().split()
            wanted = [c for c in ["cpu", "memory", "pids"] if c in available]
            if wanted:
                ctrl = " ".join(f"+{c}" for c in wanted)
                (cg / "cgroup.subtree_control").write_text(ctrl)
        except OSError:
            pass

    return agent_cg


def _cgd_create_and_join(
    container_cgroup: Path,
    name: str,
    request: dict,
    peer_pid: int,
    log_file,
) -> dict:
    """Create a child cgroup under agent/, set limits, and move the caller into it."""
    agent_cg = _cgd_ensure_agent_cgroup(container_cgroup, log_file)
    if agent_cg is None:
        return {"ok": False, "error": "Failed to set up agent cgroup hierarchy"}

    job_cg = agent_cg / name
    try:
        job_cg.mkdir(exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"Cannot create cgroup {name}: {e}"}

    errors = []

    # CPU limit: percentage of all CPUs → cpu.max (quota period)
    cpu_pct = request.get("cpu_pct")
    if cpu_pct is not None:
        try:
            pct = int(cpu_pct)
            if pct < 1 or pct > 100 * os.cpu_count():
                errors.append(f"cpu_pct out of range: {pct}")
            else:
                nproc = os.cpu_count() or 1
                quota = pct * 1000 * nproc
                (job_cg / "cpu.max").write_text(f"{quota} 100000")
        except (ValueError, OSError) as e:
            errors.append(f"cpu.max: {e}")

    # Memory limit
    memory = request.get("memory")
    if memory is not None:
        mem_bytes = _parse_memory_value(str(memory))
        if mem_bytes is None or mem_bytes < 1048576:  # min 1MB
            errors.append(f"Invalid memory value: {memory}")
        else:
            try:
                (job_cg / "memory.max").write_text(str(mem_bytes))
            except OSError as e:
                errors.append(f"memory.max: {e}")

    # PID limit
    pids = request.get("pids")
    if pids is not None:
        try:
            pids_val = int(pids)
            if pids_val < 1 or pids_val > 1000000:
                errors.append(f"pids out of range: {pids_val}")
            else:
                (job_cg / "pids.max").write_text(str(pids_val))
        except (ValueError, OSError) as e:
            errors.append(f"pids.max: {e}")

    # Move the caller into the new cgroup (peer_pid is already host-namespace)
    try:
        (job_cg / "cgroup.procs").write_text(str(peer_pid))
    except OSError as e:
        return {
            "ok": False,
            "error": f"Cannot move PID {peer_pid} into cgroup: {e}",
            "limit_errors": errors,
        }

    cg_root = Path("/sys/fs/cgroup")
    try:
        cg_path = str(job_cg.relative_to(cg_root))
    except ValueError:
        cg_path = str(job_cg)
    result = {"ok": True, "cgroup": cg_path}
    if errors:
        result["warnings"] = errors
    return result


def _cgd_destroy(container_cgroup: Path, name: str, log_file) -> dict:
    """Remove a child cgroup (must be empty of processes)."""
    agent_cg = container_cgroup / "agent"
    job_cg = agent_cg / name
    if not job_cg.exists():
        return {"ok": True}  # Already gone — idempotent
    try:
        # Check for remaining processes
        procs = (job_cg / "cgroup.procs").read_text().strip()
        if procs:
            return {
                "ok": False,
                "error": f"Cgroup {name} still has processes: {procs}",
            }
        job_cg.rmdir()
        return {"ok": True}
    except OSError as e:
        return {"ok": False, "error": f"Cannot remove cgroup {name}: {e}"}


def start_cgroup_delegate(
    cname: str, runtime: str, socket_dir: Path
) -> Optional[threading.Thread]:
    """Start the host-side cgroup delegate daemon.

    Listens on a Unix socket in socket_dir.  Returns the daemon thread, or
    None if cgroup v2 is not available on this host.

    On macOS, cgroups don't exist — the daemon is not started and resource
    limiting inside the jail is unavailable.
    """
    if IS_MACOS:
        # macOS has no cgroup v2 — skip the delegation daemon entirely.
        # The socket dir still needs to exist so the container mount succeeds.
        socket_dir.mkdir(parents=True, exist_ok=True)
        return None

    # Quick sanity: is cgroup v2 available on the host?
    if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
        return None

    socket_dir.mkdir(parents=True, exist_ok=True)
    sock_path = socket_dir / CGD_SOCKET_NAME
    sock_path.unlink(missing_ok=True)

    log_dir = GLOBAL_STORAGE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / f"{cname}-cgd.log", "a")

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    sock_path.chmod(0o777)  # Container runs as mapped UID — must be accessible
    srv.listen(8)
    srv.settimeout(1.0)  # Allow periodic shutdown checks

    container_cgroup: Optional[Path] = None
    container_cgroup_lock = threading.Lock()
    shutdown = threading.Event()

    def serve():
        nonlocal container_cgroup
        while not shutdown.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            # Lazy-resolve container cgroup on first request
            with container_cgroup_lock:
                if container_cgroup is None:
                    container_cgroup = _resolve_container_cgroup(cname, runtime)
                    if container_cgroup:
                        print(
                            f"Resolved container cgroup: {container_cgroup}",
                            file=log_file,
                            flush=True,
                        )
                    else:
                        print(
                            "WARNING: Could not resolve container cgroup",
                            file=log_file,
                            flush=True,
                        )
            if container_cgroup is None:
                try:
                    conn.sendall(
                        (
                            json.dumps(
                                {
                                    "ok": False,
                                    "error": "Container cgroup not yet available",
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    conn.close()
                except Exception:
                    pass
                continue

            _cgroup_delegate_handler(conn, container_cgroup, log_file)
        srv.close()
        log_file.close()

    t = threading.Thread(target=serve, daemon=True, name=f"cgd-{cname}")
    t._shutdown_event = shutdown  # type: ignore[attr-defined]
    t._socket = srv  # type: ignore[attr-defined]
    t.start()

    # Give the socket a moment to be ready
    time.sleep(0.05)
    return t


def stop_cgroup_delegate(
    thread: Optional[threading.Thread], socket_dir: Optional[Path]
):
    """Stop the cgroup delegate daemon and clean up."""
    if thread is not None:
        shutdown = getattr(thread, "_shutdown_event", None)
        if shutdown:
            shutdown.set()
        thread.join(timeout=3)
    if socket_dir and socket_dir.exists():
        shutil.rmtree(socket_dir, ignore_errors=True)


VALID_MCP_PRESETS = {"chrome-devtools", "sequential-thinking"}
DEFAULT_MISE_TOOLS = {"neovim": "stable"}


def _effective_mcp_server_names(
    mcp_servers: Optional[Dict[str, Any]] = None,
    mcp_presets: Optional[List[str]] = None,
) -> List[str]:
    """Return the effective MCP server names after presets + config overrides/removals."""
    # Start with preset names
    names = list(mcp_presets or [])

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


def _seed_agent_dir(src: Path, dst: Path):
    """Copy auth-related files from GLOBAL_HOME agent dir into per-workspace overlay.

    Only copies files that don't already exist in dst — the entrypoint regenerates
    configs on every boot, so we only need to seed auth tokens and similar
    persistent state on first use.  Skips subdirectories (those are created by
    the entrypoint as needed).
    """
    if not src.is_dir():
        return
    for item in src.iterdir():
        if item.is_file():
            target = dst / item.name
            if not target.exists():
                try:
                    shutil.copy2(item, target)
                except OSError:
                    pass  # permission errors on stale files — skip


def _migrate_old_overlay(old: Path, new: Path):
    """Merge data from a pre-refactor per-workspace overlay dir into the new location.

    Copies files that don't already exist in the target.  Existing files in
    ``new`` are never overwritten so user data created post-refactor wins.
    """
    if not old.is_dir() or not any(old.iterdir()):
        return
    new.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old, new, dirs_exist_ok=True, copy_function=_copy_if_missing)


def _copy_if_missing(src: str, dst: str):
    """shutil copy_function that skips existing files."""
    if not Path(dst).exists():
        shutil.copy2(src, dst)


def generate_agents_md(
    cname: str,
    workspace: Path,
    blocked_tools: List[Dict[str, str]],
    mount_descriptions: List[str],
    net_mode: str = "bridge",
    runtime: str = "podman",
    forward_host_ports: Optional[List] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
    mcp_presets: Optional[List[str]] = None,
) -> Path:
    """Generate per-workspace AGENTS.md and CLAUDE.md files and return the directory.

    Produces separate files for Copilot, Gemini, and Claude so that user-level
    ~/.copilot/AGENTS.md, ~/.gemini/AGENTS.md, and ~/.claude/CLAUDE.md content
    can differ between the agents.
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

    mcp_server_names = _effective_mcp_server_names(mcp_servers, mcp_presets)

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
            "## Resource Management",
            "",
            "The jail may have hard resource limits set by the human operator (memory, CPU, PIDs).",
            "These are kernel-enforced — exceeding memory triggers OOM kill, exceeding PIDs prevents",
            "new processes. You cannot change container-level limits, but you can enforce hard limits",
            "on your own sub-processes using `yolo-cglimit`:",
            "",
            "### yolo-cglimit (recommended for hard limits)",
            "",
            "Located at `~/.local/bin/yolo-cglimit` (on PATH). Run `yolo-cglimit --help` for usage.",
            "",
            "```bash",
            "# Limit a training job to 75% of all CPUs",
            "yolo-cglimit --cpu 75 -- python train.py",
            "",
            "# 50% CPU + 2GB RAM",
            "yolo-cglimit --cpu 50 --memory 2g -- make -j8",
            "",
            "# Max 100 processes (prevent fork bombs)",
            "yolo-cglimit --pids 100 -- ./build.sh",
            "",
            "# Named cgroup for monitoring",
            "yolo-cglimit --cpu 75 --name training -- python train.py",
            "```",
            "",
            "These limits are enforced by the kernel via cgroup v2 — they cannot be exceeded.",
            "The tool communicates with a host-side daemon over a Unix socket; no elevated",
            "privileges are needed inside the jail. If the daemon is unavailable, `yolo-cglimit`",
            "will print an error with guidance.",
            "",
            "**How it works**: The yolo CLI runs a cgroup delegate daemon on the host alongside",
            "the container. When you call `yolo-cglimit`, it sends a JSON request to the daemon",
            "via `/tmp/yolo-cgd/cgroup.sock`. The daemon creates a child cgroup in the container's",
            "cgroup tree, sets limits, and moves your process into it using SO_PEERCRED for secure",
            "PID identity. All operations are logged for auditability.",
            "",
            "**Podman is the primary supported runtime** for cgroup delegation. Docker support",
            "is best-effort.",
            "",
            "### Soft limits (always available)",
            "",
            "| Tool | Purpose | Example |",
            "|------|---------|---------|",
            "| `nice` | Lower CPU priority | `nice -n 19 python train.py` |",
            "| `ionice` | Lower I/O priority | `ionice -c 3 python train.py` |",
            "| `timeout` | Wall-clock limit | `timeout 3600 python train.py` |",
            "| `ulimit` | Per-process limits | `ulimit -v 4000000` (4GB virtual mem) |",
            "",
            "For long-running jobs (training, builds), combine limits:",
            "```bash",
            "yolo-cglimit --cpu 75 --memory 4g -- nice -n 10 timeout 7200 python train.py",
            "```",
            "",
            "To request container-level resource limit changes, edit `/workspace/yolo-jail.jsonc`:",
            "```json",
            '  "resources": {"memory": "8g", "cpus": 4, "pids_limit": 4096}',
            "```",
            "Then run `yolo check --no-build` and ask the human to restart the jail.",
            "",
            "## Skills",
            "",
            "Skills directories (`~/.copilot/skills/`, `~/.gemini/skills/`, `~/.claude/skills/`)",
            "are **read-only** (kernel-enforced). You cannot create or modify skills inside the jail.",
            "If you attempt to write, you will get a 'Read-only file system' error — this is expected.",
            "",
            "To develop a new skill: create it in `/workspace/.copilot/skills/` (or `.gemini/`, `.claude/`),",
            "test it manually, then ask the human to promote it to their host-level skills directory",
            "outside the jail. The skill will be available in all jails after the next restart.",
            "",
            "## Testing Changes to yolo-jail",
            "",
            "The `/workspace` directory is a bind mount of the host's repo. Your edits to",
            "`src/cli.py` are **immediately visible to the host** — no commit or push needed.",
            "The host's `yolo` command reads from this shared working tree.",
            "",
            "When modifying `src/cli.py` or `src/entrypoint.py`, **always verify with a nested",
            "jail** before telling the human to test on the host. Run `yolo -- bash` from inside",
            "this jail to launch a nested jail and confirm your changes work end-to-end.",
            "Container startup errors (mount failures, permission errors, read-only filesystem",
            "conflicts) are only caught by actually running the container — unit tests alone are",
            "not sufficient.",
            "",
            "**Important:** Changes to `src/cli.py` take effect on the next `yolo` invocation",
            "on the host (no rebuild needed). Changes to `src/entrypoint.py` or `flake.nix`",
            "require `just load && just install` on the host since the entrypoint is baked",
            "into the Nix image.",
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

    # Claude reads ~/.claude/CLAUDE.md (not AGENTS.md) at the user-config level.
    user_claude = home / ".claude" / "CLAUDE.md"
    if user_claude.exists():
        claude_content = user_claude.read_text() + "\n---\n\n" + jail_content
    else:
        claude_content = jail_content
    (agents_dir / "CLAUDE.md").write_text(claude_content)

    return agents_dir


# ---------------------------------------------------------------------------
# Skills merging (host-side, for :ro bind mounts)
# ---------------------------------------------------------------------------

_BUILTIN_JAIL_STARTUP_SKILL = """\
---
name: jail-startup
description: First-run skill for agents entering a YOLO Jail. Reads the handover document left by the outer agent and orients you to the jail environment. Invoke this skill immediately when starting a new session inside a jail.
---

# Jail Startup

You are running inside a **YOLO Jail** — an isolated container environment.
This skill helps you pick up where the previous (outer) agent left off.

## Step 1: Read the Handover Document

The outer agent was REQUIRED to write a handover document before you were
launched. Read it now:

**Primary location:** `.yolo/handover.md` (i.e., `/workspace/.yolo/handover.md`)

If it exists, read it carefully — it contains:
- What the outer agent was working on
- What remains to be done
- Key decisions and rationale
- Files to look at first
- Gotchas and context you need

If the file does NOT exist, tell the human:
> "No handover document found at `.yolo/handover.md`. The outer agent should
> have created one. Can you tell me what I should be working on?"

## Step 2: Orient Yourself

Key facts about your environment:
- **Workspace** is at `/workspace` — this is the SAME directory as on the host (bind-mounted read-write). Changes you make are immediately visible on the host.
- **Internet** is available. You can curl, pip install, npm install, etc.
- **Home** is `/home/agent` — shared across ALL jail workspaces. Auth tokens, tool caches, and configs persist here.
- **Tools**: git, rg, fd, bat, jq, nvim, curl, gh, uv, mise, tmux, and more.
- **Runtimes**: Node.js, Python, Go (managed by mise).
- **Blocked tools**: Some tools may be shimmed (e.g., grep → rg). Check AGENTS.md or run `ls ~/.yolo-shims/` if you hit unexpected blocks. Set `YOLO_BYPASS_SHIMS=1` for scripts that need originals.
- **No pagers**: `PAGER=cat`. Never pipe to `less` or `more`.
- Run `yolo config-ref` for full configuration and environment reference.

## Step 3: Execute

After reading the handover document, proceed with the tasks described in it.
You have full capability — treat this as your primary working environment.
"""


def _prepare_skills(cname: str, workspace: Path) -> Path:
    """Prepare merged skills directory on the host for :ro bind mounting.

    Merge order (later overrides earlier):
      1. Built-in skills (jail-startup)
      2. Host user-level skills (~/.gemini/skills/, ~/.copilot/skills/, ~/.claude/skills/)
      3. Workspace skills (<workspace>/.copilot/skills/, .gemini/skills/, .claude/skills/)

    Returns the staging directory containing skills-copilot/, skills-gemini/, skills-claude/.
    """
    staging = AGENTS_DIR / cname
    staging.mkdir(parents=True, exist_ok=True)

    # Collect host user-level skill sources
    home = Path.home()
    host_skill_dirs = []
    for dotdir in (".copilot", ".gemini", ".claude"):
        p = home / dotdir / "skills"
        if p.is_dir():
            host_skill_dirs.append(p)

    # Collect workspace skill sources
    ws_skill_dirs = []
    for dotdir in (".copilot", ".gemini", ".claude"):
        p = workspace / dotdir / "skills"
        if p.is_dir():
            ws_skill_dirs.append(p)

    for agent_suffix in ("copilot", "gemini", "claude"):
        skills_dir = staging / f"skills-{agent_suffix}"
        # Clean slate each time
        if skills_dir.exists():
            shutil.rmtree(skills_dir)
        skills_dir.mkdir()

        # 1. Built-in skills
        builtin = skills_dir / "jail-startup"
        builtin.mkdir()
        (builtin / "SKILL.md").write_text(_BUILTIN_JAIL_STARTUP_SKILL)

        # 2. Host user-level skills (all agent dirs merged)
        for src_dir in host_skill_dirs:
            _copy_skill_subdirs(src_dir, skills_dir)

        # 3. Workspace skills (highest priority, all agent dirs merged)
        for src_dir in ws_skill_dirs:
            _copy_skill_subdirs(src_dir, skills_dir)

    return staging


def _copy_skill_subdirs(src: Path, dst: Path):
    """Copy skill subdirectories from src into dst, following symlinks."""
    if not src.is_dir():
        return
    for item in src.iterdir():
        if item.is_dir():
            target = dst / item.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target, symlinks=False)


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


def _image_cache_path(store_path: str) -> Path:
    """Return the cached tar file path for a nix store path.

    Images are cached in GLOBAL_CACHE/images/ keyed by a hash of the store path.
    Using a file lets ``podman load -i`` detect existing layers and skip them,
    which is ~30x faster than streaming through a pipe when layers are shared
    across project configs.
    """
    cache_dir = GLOBAL_CACHE / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path_hash = hashlib.sha256(store_path.encode()).hexdigest()[:16]
    return cache_dir / f"{path_hash}.tar"


def _stream_image_command(store_path: str) -> list[str]:
    """Return the command to stream the Docker image tarball to stdout.

    On macOS the streaming script has a Linux shebang and cannot execute
    locally.  If a remote builder is configured in ``/etc/nix/machines``,
    we first ``nix copy`` the closure to the builder, then execute the
    script there via SSH.  Falls back to local execution (Linux hosts).
    """
    if not IS_MACOS:
        return [store_path]

    machines_file = Path("/etc/nix/machines")
    if not machines_file.exists():
        # Fallback: try local execution (will likely fail)
        return [store_path]

    for line in machines_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2 and "linux" in parts[1]:
            builder_uri = parts[0]  # e.g. ssh-ng://nix-builder
            # Derive the SSH host from the URI
            ssh_host = builder_uri.replace("ssh-ng://", "").replace("ssh://", "")
            # Copy the closure to the builder
            copy_result = subprocess.run(
                ["nix", "copy", "--to", builder_uri, store_path],
                capture_output=True,
                timeout=300,
            )
            if copy_result.returncode != 0:
                # nix copy failed — fall back to local execution
                return [store_path]
            return ["ssh", ssh_host, store_path]

    return [store_path]


def _materialize_image(store_path: str, cache_file: Path, status) -> int:
    """Stream the nix image to a cache tar file.  Returns byte count."""
    sentinel = BUILD_DIR / "last-load-size"
    estimated_size = _estimate_image_size(store_path, sentinel)

    status.update("[bold cyan]Materializing image to cache...")
    stream_cmd = _stream_image_command(store_path)
    stream_proc = subprocess.Popen(
        stream_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    total_bytes = 0
    chunk_size = 1024 * 1024  # 1 MB
    tmp_file = cache_file.with_suffix(".tmp")
    try:
        with open(tmp_file, "wb") as f:
            while True:
                chunk = stream_proc.stdout.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                total_bytes += len(chunk)
                progress = _format_progress(total_bytes, estimated_size)
                status.update(f"[bold cyan]Caching image... {progress}")
        stream_proc.wait()
        if stream_proc.returncode != 0:
            tmp_file.unlink(missing_ok=True)
            return 0
        tmp_file.rename(cache_file)
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise

    # Save size for future estimates
    size_file = BUILD_DIR / "last-load-size"
    size_file.write_text(str(total_bytes))
    return total_bytes


def auto_load_image(
    repo_root: Path,
    extra_packages: Optional[List[Union[str, dict]]] = None,
    runtime: str = "docker",
):
    """Cheaply check if the nix image needs to be reloaded into the container runtime."""
    # Per-runtime sentinel tracks all store paths loaded into this runtime
    sentinel = BUILD_DIR / f"last-load-{runtime}"
    # Use a PID-unique out-link to avoid races when multiple jails build concurrently
    out_link = BUILD_DIR / f"run-result-{os.getpid()}"
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
        # If the image already exists in the runtime, proceed.
        image_name = _jail_image(runtime)
        check = subprocess.run(
            _image_inspect_cmd(runtime, image_name),
            capture_output=True,
        )
        if check.returncode == 0:
            console.print(f"[yellow]Using existing {image_name} image.[/yellow]")
            return
        # No image in runtime — try loading from the most recent cached tar.
        # This handles nested jails where nix build fails but the host already
        # cached the image tar in the shared GLOBAL_CACHE.
        cache_dir = GLOBAL_CACHE / "images"
        if cache_dir.is_dir():
            tars = sorted(
                cache_dir.glob("*.tar"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            for tar_file in tars:
                console.print(
                    f"[yellow]Loading image from cache: {tar_file.name}[/yellow]"
                )
                if runtime == "container":
                    if _load_image_for_apple_container(str(tar_file), console):
                        console.print(
                            "[bold green]Done: loaded image from cache[/bold green]"
                        )
                        return
                else:
                    load_result = subprocess.run(
                        _image_load_cmd(runtime, str(tar_file)),
                        capture_output=True,
                    )
                    if load_result.returncode == 0:
                        console.print(
                            "[bold green]Done: loaded image from cache[/bold green]"
                        )
                        return
        console.print(
            f"[bold red]No existing {image_name} image found. Cannot start jail.[/bold red]"
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
                # Materialize the nix image to a cached tar file (or reuse existing).
                # Using a file lets `podman load -i` detect existing layers and skip
                # them (~1-2s), vs piping which must transfer all bytes (~30-40s).
                cache_file = _image_cache_path(current_path)
                if not cache_file.exists():
                    total_bytes = _materialize_image(current_path, cache_file, status)
                    if total_bytes == 0:
                        console.print(
                            "[bold red]Error streaming image to cache.[/bold red]"
                        )
                        out_link.unlink(missing_ok=True)
                        return
                    mb = total_bytes / (1024 * 1024)
                    size_str = f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"
                    console.print(f"  [dim]Cached image: {size_str}[/dim]")

                # Load from cached file — podman detects existing layers and skips them
                load_ok = False
                load_result = None
                if runtime == "container":
                    load_ok = _load_image_for_apple_container(
                        str(cache_file), console, status
                    )
                else:
                    status.update(f"[bold cyan]Loading image into {runtime}...")
                    load_result = subprocess.run(
                        _image_load_cmd(runtime, str(cache_file)),
                        capture_output=True,
                    )
                    load_ok = load_result.returncode == 0

            if not load_ok:
                if runtime != "container":
                    console.print(
                        f"[bold red]Error loading image into {runtime}.[/bold red]"
                    )
                    stderr = load_result.stderr.decode().strip()
                    if stderr:
                        console.print(f"  [dim]{stderr}[/dim]")
            else:
                _add_loaded_path(sentinel, current_path)
                console.print("[bold green]Done: loaded image[/bold green]")
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


DEFAULT_HOST_CLAUDE_FILES = ["settings.json", ".credentials.json"]

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
    "mcp_presets",
    "devices",
    "gpu",
    "resources",
    "env",
    "host_claude_files",
}
KNOWN_NETWORK_KEYS = {"mode", "ports", "forward_host_ports"}
KNOWN_SECURITY_KEYS = {"blocked_tools"}
KNOWN_BLOCKED_TOOL_KEYS = {"name", "message", "suggestion"}
KNOWN_PACKAGE_KEYS = {"name", "nixpkgs", "version", "url", "hash"}
KNOWN_LSP_SERVER_KEYS = {"command", "args", "fileExtensions"}
KNOWN_MCP_SERVER_KEYS = {"command", "args"}
KNOWN_DEVICE_KEYS = {"usb", "description", "cgroup_rule"}
KNOWN_GPU_KEYS = {"enabled", "devices", "capabilities"}
KNOWN_RESOURCES_KEYS = {"memory", "cpus", "pids_limit"}
USB_ID_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{4}$")
MEMORY_RE = re.compile(r"^\d+[bkmgBKMG]?$")


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


def _check_preset_null_conflicts(config: Dict[str, Any], label: str) -> List[str]:
    """Report same-file preset/null contradictions.

    Cross-hierarchy conflicts (user-level preset + workspace-level null) are
    valid and intentional, so this only checks within a single config file.
    """
    errors: List[str] = []
    presets = config.get("mcp_presets")
    servers = config.get("mcp_servers")
    if not isinstance(presets, list) or not isinstance(servers, dict):
        return errors
    for name in presets:
        if isinstance(name, str) and name in servers and servers[name] is None:
            errors.append(
                f"{label}: preset '{name}' is enabled in mcp_presets but "
                f"null-removed in mcp_servers within the same config file"
            )
    return errors


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

    host_claude_files = config.get("host_claude_files")
    if host_claude_files is not None:
        if not isinstance(host_claude_files, list):
            errors.append("config.host_claude_files: expected a list of strings")
        else:
            for idx, entry in enumerate(host_claude_files):
                if not isinstance(entry, str):
                    errors.append(f"config.host_claude_files[{idx}]: expected a string")
                elif "/" in entry or "\\" in entry:
                    errors.append(
                        f"config.host_claude_files[{idx}]: must be a filename, not a path"
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

    mcp_presets = config.get("mcp_presets")
    if mcp_presets is not None:
        if not isinstance(mcp_presets, list):
            errors.append("config.mcp_presets: expected an array of preset names")
        else:
            for idx, name in enumerate(mcp_presets):
                if not isinstance(name, str):
                    errors.append(f"config.mcp_presets[{idx}]: expected a string")
                elif name not in VALID_MCP_PRESETS:
                    errors.append(
                        f"config.mcp_presets[{idx}]: unknown preset '{name}'. "
                        f"Valid presets: {', '.join(sorted(VALID_MCP_PRESETS))}"
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

    # GPU config validation
    gpu = config.get("gpu")
    if gpu is not None:
        if not isinstance(gpu, dict):
            errors.append("config.gpu: expected an object")
        else:
            _report_unknown_keys(gpu, KNOWN_GPU_KEYS, "config.gpu", errors)
            enabled = gpu.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append("config.gpu.enabled: expected a boolean")
            devices_val = gpu.get("devices")
            if devices_val is not None:
                if not isinstance(devices_val, str):
                    errors.append(
                        "config.gpu.devices: expected a string ('all', '0', '0,1', or 'GPU-<uuid>')"
                    )
            capabilities = gpu.get("capabilities")
            if capabilities is not None:
                if not isinstance(capabilities, str):
                    errors.append(
                        "config.gpu.capabilities: expected a string (e.g. 'compute,utility')"
                    )
                else:
                    valid_caps = {
                        "compute",
                        "utility",
                        "graphics",
                        "video",
                        "display",
                        "compat32",
                    }
                    for cap in capabilities.split(","):
                        cap = cap.strip()
                        if cap and cap not in valid_caps:
                            errors.append(
                                f"config.gpu.capabilities: unknown capability '{cap}'. "
                                f"Valid: {', '.join(sorted(valid_caps))}"
                            )

    # Resources config validation
    resources = config.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            errors.append("config.resources: expected an object")
        else:
            _report_unknown_keys(
                resources, KNOWN_RESOURCES_KEYS, "config.resources", errors
            )
            memory = resources.get("memory")
            if memory is not None:
                if not isinstance(memory, str):
                    errors.append(
                        "config.resources.memory: expected a string (e.g. '8g', '512m')"
                    )
                elif not MEMORY_RE.match(memory):
                    errors.append(
                        "config.resources.memory: invalid format. "
                        "Use a number with optional suffix: b, k, m, g (e.g. '8g', '512m')"
                    )
            cpus = resources.get("cpus")
            if cpus is not None:
                if isinstance(cpus, (int, float)):
                    if cpus <= 0:
                        errors.append(
                            "config.resources.cpus: must be a positive number"
                        )
                elif isinstance(cpus, str):
                    try:
                        val = float(cpus)
                        if val <= 0:
                            errors.append(
                                "config.resources.cpus: must be a positive number"
                            )
                    except ValueError:
                        errors.append(
                            "config.resources.cpus: expected a number (e.g. 4, 2.5, '0.5')"
                        )
                else:
                    errors.append(
                        "config.resources.cpus: expected a number (e.g. 4, 2.5, '0.5')"
                    )
            pids_limit = resources.get("pids_limit")
            if pids_limit is not None:
                if not isinstance(pids_limit, int) or pids_limit <= 0:
                    errors.append(
                        "config.resources.pids_limit: expected a positive integer"
                    )

    # env validation
    env_vars = config.get("env")
    if env_vars is not None:
        if not isinstance(env_vars, dict):
            errors.append("config.env: expected an object of key-value string pairs")
        else:
            for key, value in env_vars.items():
                if not isinstance(key, str) or not key:
                    errors.append("config.env: keys must be non-empty strings")
                elif not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    errors.append(
                        f"config.env.{key}: invalid variable name "
                        "(must match [A-Za-z_][A-Za-z0-9_]*)"
                    )
                if not isinstance(value, str):
                    errors.append(f"config.env.{key}: expected a string value")

    return errors, warnings


def _runtime_for_check(config: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Resolve the effective runtime without exiting.

    Same platform-aware priority as _runtime():
      macOS: container → podman → docker
      Linux: podman → docker

    Only returns runtimes whose daemon is actually reachable.
    """
    env = os.environ.get("YOLO_RUNTIME")
    if env and env in ("podman", "docker", "container"):
        if shutil.which(env):
            if _runtime_is_connectable(env):
                return env, None
            return (
                None,
                f"Configured runtime '{env}' from YOLO_RUNTIME is not connected",
            )
        return None, f"Configured runtime '{env}' from YOLO_RUNTIME is not on PATH"

    cfg = config.get("runtime")
    if cfg and cfg in ("podman", "docker", "container"):
        if shutil.which(cfg):
            if _runtime_is_connectable(cfg):
                return cfg, None
            return (
                None,
                f"Configured runtime '{cfg}' from yolo-jail.jsonc is not connected",
            )
        return None, f"Configured runtime '{cfg}' from yolo-jail.jsonc is not on PATH"

    if IS_MACOS:
        candidates = ("container", "podman", "docker")
    else:
        candidates = ("podman", "docker")
    for rt in candidates:
        path = shutil.which(rt)
        if path:
            if rt == "container" and not _is_apple_container(path):
                continue
            if not _runtime_is_connectable(rt):
                continue
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
                "YOLO_MCP_PRESETS": json.dumps(config.get("mcp_presets", [])),
            }
        )
        # Apply user-defined env vars from config
        for env_key, env_val in config.get("env", {}).items():
            env[env_key] = env_val

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
entrypoint.configure_claude()

json.loads((entrypoint.COPILOT_DIR / "mcp-config.json").read_text())
json.loads((entrypoint.COPILOT_DIR / "lsp-config.json").read_text())
json.loads((entrypoint.GEMINI_DIR / "settings.json").read_text())
json.loads((entrypoint.CLAUDE_DIR / "settings.json").read_text())
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
  // Container runtime: "podman", "docker", or "container" (Apple)
  // (also settable via YOLO_RUNTIME env var)
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

  // Extra environment variables set inside the jail.
  // Keys are variable names, values are strings.
  // "env": {"DATABASE_URL": "postgres://localhost/dev", "DEBUG": "1"}

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
  // Enable built-in MCP server presets by name.
  // Available presets: chrome-devtools, sequential-thinking
  // "mcp_presets": ["chrome-devtools", "sequential-thinking"]

  // Additional custom MCP servers for Copilot and Gemini.
  // Add custom servers or set a preset/inherited server to null to disable it.
  // Binary must be on PATH or absolute.
  // "mcp_servers": {
  //   "my-custom": {
  //     "command": "/workspace/scripts/my-mcp-server.py",
  //     "args": []
  //   }
  // }

  // NVIDIA GPU passthrough. Requires NVIDIA Container Toolkit on the host.
  // Run "yolo check" to verify GPU readiness before enabling.
  // "gpu": {
  //   "enabled": true,
  //   "devices": "all",          // "all", "0", "0,1", or "GPU-<uuid>"
  //   "capabilities": "compute,utility"
  // }

  // Container resource limits.
  // On Apple Container: applied as VM hardware limits (defaults: half host CPUs/RAM).
  // On Docker/Podman: applied as --cpus/--memory flags (no defaults — inherits VM limits).
  // On Linux: also feeds cgroup delegation for in-container yolo-cglimit.
  // "resources": {
  //   "memory": "8g",            // Max memory (b/k/m/g suffix). OOM-killed if exceeded.
  //   "cpus": 4,                 // CPU limit (decimal). e.g. 4, 2.5, "0.5"
  //   "pids_limit": 4096         // Max processes (Docker/Podman only). Prevents fork bombs.
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
     Tell them to run: yolo -- copilot  (or yolo -- gemini, yolo -- claude)

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
  // Container runtime: "podman", "docker", or "container" (Apple)
  // (also settable via YOLO_RUNTIME env var)
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

  [bold]host_claude_files[/bold] (array of strings): Host ~/.claude/ files to sync into the jail.
    Each entry is a filename (not a path) relative to ~/.claude/.
    Files are mounted read-only at /ctx/host-claude/ and copied into the jail's
    ~/.claude/ on startup. For settings.json, host settings are deep-merged with
    YOLO-required overrides (YOLO wins on conflicts).
    The fileSuggestion script referenced in host settings.json is auto-discovered
    and synced (if it lives under ~/.claude/) — no need to list it explicitly.
    Default: ["settings.json"]
    Set to [] to disable host claude file syncing.
    Example: ["settings.json", "keybindings.json"]

  [bold]env[/bold] (object): Extra environment variables set inside the jail.
    Keys are variable names, values are strings.
    Merged: user config provides defaults, workspace config overrides.
    These are set as container-level env vars (visible to all processes).
    Example: {"DATABASE_URL": "postgres://localhost/dev", "DEBUG": "1"}

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

  [bold]lsp_servers[/bold] (object): Additional language servers for Copilot and Gemini (Claude uses its own tools).
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

  [bold]mcp_presets[/bold] (array of strings): Enable built-in MCP server presets by name.
    No presets are enabled by default. Available presets:
      • chrome-devtools: Headless Chromium automation via Chrome DevTools Protocol.
      • sequential-thinking: Chain-of-thought reasoning via MCP.
    Invalid: enabling a preset here and null-removing it in the same config file.
    Example: ["chrome-devtools", "sequential-thinking"]

  [bold]mcp_servers[/bold] (object): Custom MCP servers for Copilot, Gemini, and Claude.
    Add custom servers, or set a preset/inherited server to [bold]null[/bold] to disable it.
    Each key is a server name; value is an object with:
      • command (string, required): Binary name (on PATH) or absolute path.
      • args (array of strings): Args passed to the MCP server. Default: [].
    The entrypoint translates these for each agent:
      • Copilot: written to a per-workspace overlay mounted at ~/.copilot/mcp-config.json.
      • Gemini: written to a per-workspace overlay mounted at ~/.gemini/settings.json.
      • Claude: written to a per-workspace overlay mounted at ~/.claude/settings.json.
    Example: {"my-custom": {"command": "/workspace/scripts/my-mcp.py", "args": []}}

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

  [bold]gpu[/bold] (object): NVIDIA GPU passthrough configuration.
    Requires NVIDIA Container Toolkit on the host.
    • [bold]enabled[/bold] (bool): Enable GPU passthrough. Default: false.
    • [bold]devices[/bold] (string): Which GPUs to expose. Default: "all".
      Values: "all", "0", "0,1", or "GPU-<uuid>".
      Docker uses --gpus flag; Podman uses CDI (nvidia.com/gpu=...).
    • [bold]capabilities[/bold] (string): NVIDIA driver capabilities. Default: "compute,utility".
      Valid: compute, utility, graphics, video, display, compat32.
      "compute,utility" is sufficient for PyTorch/CUDA training.

    Host prerequisites:
      1. NVIDIA driver installed (nvidia-smi works)
      2. nvidia-container-toolkit installed
      3. Docker: sudo nvidia-ctk runtime configure --runtime=docker
         Podman: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
    Run [bold]yolo check[/bold] to verify GPU readiness.
    Subject to config change safety (human approval required).

  [bold]resources[/bold] (object): Container resource limits.
    Sets hard cgroup constraints on the jail container via Docker/Podman flags.
    These limits are enforced by the kernel — the jail cannot exceed them.
    • [bold]memory[/bold] (string): Maximum memory. Format: number + suffix (b/k/m/g).
      Examples: "8g" (8 GB), "512m" (512 MB), "2g".
      Maps to --memory flag. OOM-killed if exceeded.
    • [bold]cpus[/bold] (number|string): CPU limit as a decimal. Default: no limit.
      Examples: 4 (four cores), 2.5 (two and a half cores), "0.5" (half a core).
      Maps to --cpus flag (CFS quota).
    • [bold]pids_limit[/bold] (integer): Maximum number of processes. Default: 32768 (Podman's built-in default of 2048 is too low for agent workloads).
      Prevents fork bombs and runaway process creation.
      Maps to --pids-limit flag.

    [bold]In-jail sub-process limits (cgroup v2 delegation)[/bold]:
    A host-side cgroup delegate daemon runs alongside the container and
    performs all privileged cgroup operations on behalf of agents inside the
    jail.  No CAP_SYS_ADMIN or writable cgroup mount is needed inside the
    container — the daemon validates every request and operates securely on
    the host cgroup filesystem via a Unix socket.
    Use the [bold]yolo-cglimit[/bold] helper inside the jail:
      yolo-cglimit --cpu 75 -- python train.py           # 75% of all CPUs
      yolo-cglimit --cpu 50 --memory 2g -- make -j8      # 50% CPU + 2GB RAM
      yolo-cglimit --pids 100 -- ./script.sh             # Max 100 processes
    The daemon is started automatically by the yolo CLI.  Podman is the
    primary supported runtime; Docker support is best-effort.
    Falls back to nice/timeout/ulimit if delegation is unavailable.
    Subject to config change safety (human approval required).

[bold cyan]EXAMPLE CONFIG[/bold cyan]

  {
    "runtime": "podman",
    "mise_tools": {"neovim": "nightly"},
    "mcp_presets": ["chrome-devtools"],
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
    "env": {"MY_API_KEY": "...", "DEBUG": "1"},
    "mounts": ["/path/to/ref-repo"],
    "devices": [
      {"usb": "0bda:2838", "description": "RTL-SDR Blog V4"}
    ],
    "gpu": {
      "enabled": true,
      "devices": "all",
      "capabilities": "compute,utility"
    },
    "resources": {
      "memory": "8g",
      "cpus": 4,
      "pids_limit": 4096
    },
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

  YOLO_RUNTIME          Override container runtime (podman/docker/container)
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
    Contains: auth tokens (gh, gemini, claude), tool caches, npm/go globals,
    nvim config, shell configs, mise tool data. All of this survives
    jail restarts and is shared between every project's jail.

  [bold]Per-Workspace State[/bold]
    Some state is isolated per-workspace (in <workspace>/.yolo/):
    SSH keys, bash history, copilot sessions, gemini history, claude projects.
    These are NOT shared across different project jails.

  [bold]Identity & Auth[/bold]
    Git/jj identity (name + email) is injected from the host automatically.
    GitHub CLI (gh) is pre-authenticated via the shared home.
    SSH keys are per-workspace — configure in <workspace>/.yolo/home/ssh/.

  [bold]Tools & Runtimes[/bold]
    Runtimes: Node.js 22, Python 3.13, Go (managed by mise)
    Editors:  nvim (version configurable via mise_tools config)
    CLI:      rg, fd, bat, jq, git, jj, gh, curl, strace, uv, tmux
    Agents:   copilot, gemini (--yolo auto-injected), claude (YOLO mode via settings.json)
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
  5. Start your agent: 'yolo -- copilot', 'yolo -- gemini', or 'yolo -- claude'

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
    """Validate environment, config, and build. Run after every config edit."""
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

    # Show version for debugging
    ver = _git_describe_version() or "unknown"
    console.print(f"[dim]Version: {ver}[/dim]\n")

    # --- Environment Health ---

    console.print("[bold]Container Runtime[/bold]")
    detected_runtime = None
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
                # Verify the daemon is actually reachable, not just the CLI
                ping = subprocess.run(
                    [rt, "info"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if ping.returncode == 0:
                    ok(f"{rt}: {version}")
                    if detected_runtime is None:
                        detected_runtime = rt
                else:
                    warn(
                        f"{rt}: {version} (not connected)",
                        f"Run '{rt} info' to diagnose",
                    )
            except Exception as e:
                fail(f"{rt} found but not working: {e}")
    if detected_runtime is None:
        fail("No container runtime found", "Install podman or docker")
    console.print()

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

    if IS_MACOS and nix_path:
        # Nix daemon store connectivity (catches determinate-nixd trust bug)
        try:
            result = subprocess.run(
                ["nix", "store", "info"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            # nix store info writes its output to stderr (not stdout)
            output = result.stdout + result.stderr
            if result.returncode == 0 and "Trusted: 1" in output:
                ok("Nix daemon: connected, user is trusted")
            elif result.returncode == 0:
                fail(
                    "Nix daemon: connected but user is NOT trusted",
                    "Add your user to trusted-users in /etc/nix/nix.custom.conf "
                    "and restart the Nix daemon",
                )
            else:
                fail(
                    "Nix daemon: connection failed",
                    result.stderr.strip().split("\n")[0] if result.stderr else "",
                )
        except subprocess.TimeoutExpired:
            fail(
                "Nix daemon: store operation timed out (daemon may be hung)",
                "This is a known issue with determinate-nixd. "
                "Try: sudo launchctl kickstart -k system/systems.determinate.nix-daemon "
                "or switch to the vanilla nix-daemon",
            )
        except Exception as e:
            warn(f"Could not verify Nix daemon connectivity: {e}")

        # Check for Linux builder (required for cross-building images)
        try:
            machines_file = Path("/etc/nix/machines")
            cfg_result = subprocess.run(
                ["nix", "show-config"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            has_builder = False
            if cfg_result.returncode == 0:
                for line in cfg_result.stdout.split("\n"):
                    if line.startswith("builders =") and "@" in line:
                        if machines_file.exists() and machines_file.read_text().strip():
                            has_builder = True
                    if line.startswith("extra-platforms =") and "linux" in line:
                        warn(
                            "extra-platforms includes linux — builds will fail locally",
                            "Remove 'extra-platforms = aarch64-linux' from "
                            "/etc/nix/nix.custom.conf; use a remote builder instead",
                        )
            if has_builder:
                ok("Linux builder configured in /etc/nix/machines")
            else:
                warn(
                    "No Linux builder configured",
                    "Image builds require a Linux builder. See docs/macos.md "
                    "for setup with Colima or a remote Linux host",
                )
        except Exception:
            pass
    console.print()

    if IS_MACOS:
        console.print("[bold]macOS Platform[/bold]")
        ok(f"Architecture: {platform.machine()}")

        # Container VM backend check
        for vm_backend in ("colima", "podman"):
            vm_path = shutil.which(vm_backend)
            if vm_path:
                try:
                    if vm_backend == "colima":
                        result = subprocess.run(
                            ["colima", "status"],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if result.returncode == 0:
                            ok("Colima: running")
                        else:
                            warn(
                                "Colima installed but not running",
                                "Start with: colima start --arch aarch64 --cpu 4 --memory 8",
                            )
                    else:
                        result = subprocess.run(
                            ["podman", "machine", "info"],
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if result.returncode == 0:
                            ok("Podman Machine: available")
                        else:
                            warn("Podman Machine: not configured")
                except Exception as e:
                    warn(f"{vm_backend}: {e}")

        # Apple Container CLI check (native macOS container runtime)
        container_path = shutil.which("container")
        if container_path:
            try:
                result = subprocess.run(
                    ["container", "system", "status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    ok("Apple Container CLI: available")
                    if "running" in result.stdout.lower():
                        ok("Apple Container system: running")
                    else:
                        warn(
                            "Apple Container system not running",
                            "Start with: container system start",
                        )
                else:
                    warn(
                        "Apple Container CLI: installed but not working",
                        "Start with: container system start",
                    )
            except Exception as e:
                warn(f"Apple Container CLI: {e}")

        # OCI conversion tool check (for Apple Container image loading)
        if container_path:
            if shutil.which("skopeo"):
                ok("skopeo: available (OCI image conversion, no daemon needed)")
            elif shutil.which("docker") or shutil.which("podman"):
                ok(
                    "OCI conversion: via docker/podman (skopeo recommended: brew install skopeo)"
                )
            else:
                warn(
                    "No OCI conversion tool for Apple Container",
                    "Install skopeo (recommended): brew install skopeo",
                )

        # Nix store volume check
        nix_mount = Path("/nix")
        if nix_mount.exists():
            try:
                result = subprocess.run(
                    ["mount"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                nix_line = [
                    line
                    for line in result.stdout.split("\n")
                    if " /nix " in line or " on /nix" in line
                ]
                if nix_line:
                    if "apfs" in nix_line[0].lower():
                        ok("Nix store: mounted (APFS volume)")
                    else:
                        ok("Nix store: mounted")
                else:
                    warn(
                        "Nix store: /nix exists but mount not detected",
                        "Check /etc/synthetic.conf and Disk Utility",
                    )
            except Exception:
                ok("Nix store: /nix exists")
        else:
            fail(
                "Nix store: /nix not found",
                "Reinstall Nix or check /etc/synthetic.conf",
            )

        console.print()

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

    # --- Config Validation ---

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
        flake = repo_root / "flake.nix"
        if flake.exists():
            ok(f"flake.nix found: {flake}")
        else:
            warn(f"flake.nix not found at {flake}")
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

    # Check individual config files for same-file preset+null contradictions.
    # Cross-hierarchy overrides are valid; same-file contradictions are errors.
    for label, cfg in [
        (str(USER_CONFIG_PATH), user_config),
        ("yolo-jail.jsonc", workspace_config),
    ]:
        errors.extend(_check_preset_null_conflicts(cfg, label))

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

    # --- Entrypoint Dry-Run ---

    console.print("[bold]Entrypoint Dry-Run[/bold]")
    try:
        if repo_root is None:
            raise ConfigError("repo root resolution failed")
        if not (repo_root / "src" / "entrypoint.py").exists():
            raise ConfigError(f"entrypoint source not found under {repo_root}")
        _entrypoint_preflight(repo_root, workspace, config)
        ok("Generated Copilot/Gemini/Claude jail config in a temp home")
    except (ConfigError, SystemExit) as e:
        fail("Entrypoint preflight failed", str(e))
    console.print()

    # --- GPU Checks ---

    gpu_config = config.get("gpu", {})
    if gpu_config.get("enabled", False):
        console.print("[bold]GPU (NVIDIA)[/bold]")
        if IS_MACOS:
            warn(
                "GPU passthrough is not supported on macOS",
                "NVIDIA GPU passthrough requires Linux with NVIDIA drivers",
            )
            console.print()
        else:
            # Check nvidia-smi
            nvidia_smi = shutil.which("nvidia-smi")
            if nvidia_smi:
                try:
                    result = subprocess.run(
                        [
                            "nvidia-smi",
                            "--query-gpu=name,driver_version",
                            "--format=csv,noheader",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        for line in result.stdout.strip().split("\n"):
                            ok(f"GPU detected: {line.strip()}")
                    else:
                        fail(
                            "nvidia-smi found but no GPUs detected",
                            "Check NVIDIA driver installation",
                        )
                except Exception as e:
                    fail("nvidia-smi execution failed", str(e))
            else:
                fail(
                    "nvidia-smi not found",
                    "Install NVIDIA drivers: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/install-nvidia-driver.html",
                )

            # Check nvidia-ctk
            nvidia_ctk = shutil.which("nvidia-ctk")
            if nvidia_ctk:
                ok("nvidia-ctk found (NVIDIA Container Toolkit)")
            else:
                fail(
                    "nvidia-ctk not found",
                    "Install: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html",
                )

            # Runtime-specific checks
            effective_runtime, _ = _runtime_for_check(config)
            if effective_runtime == "podman":
                # GPU+Podman requires runc (CDI device injection fails with crun,
                # see https://github.com/containers/podman/issues/27483)
                runc_path = shutil.which("runc")
                if runc_path:
                    ok("runc found (required for Podman GPU passthrough)")
                else:
                    fail(
                        "runc not found",
                        "GPU passthrough requires runc (CDI fails with crun). "
                        "Install runc: https://github.com/opencontainers/runc/releases",
                    )

                # Check CDI spec exists
                cdi_paths = [
                    Path("/etc/cdi/nvidia.yaml"),
                    Path("/var/run/cdi/nvidia.yaml"),
                ]
                cdi_found = None
                for p in cdi_paths:
                    if p.exists():
                        cdi_found = p
                        break
                if cdi_found:
                    ok("CDI spec found for Podman GPU support")
                    # Check CDI spec driver version matches installed driver
                    try:
                        cdi_text = cdi_found.read_text()
                        # nvidia-smi driver version from earlier check
                        smi_result = subprocess.run(
                            [
                                "nvidia-smi",
                                "--query-gpu=driver_version",
                                "--format=csv,noheader",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        if smi_result.returncode == 0:
                            smi_driver = (
                                smi_result.stdout.strip().split("\n")[0].strip()
                            )
                            if smi_driver and smi_driver in cdi_text:
                                ok(f"CDI spec matches driver {smi_driver}")
                            elif smi_driver:
                                warn(
                                    f"CDI spec may be stale (driver is {smi_driver})",
                                    "Regenerate: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml",
                                )
                    except Exception:
                        pass  # Non-critical check
                else:
                    fail(
                        "No CDI spec found for Podman",
                        "Generate with: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml",
                    )
            elif effective_runtime == "docker":
                # Check Docker NVIDIA runtime configured
                try:
                    result = subprocess.run(
                        ["docker", "info", "--format", "{{.Runtimes}}"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0 and "nvidia" in result.stdout.lower():
                        ok("Docker NVIDIA runtime configured")
                    else:
                        warn(
                            "Docker NVIDIA runtime may not be configured",
                            "Run: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker",
                        )
                except Exception:
                    warn("Could not verify Docker NVIDIA runtime configuration")
            console.print()

    # --- Image & Containers ---

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

    if detected_runtime:
        console.print("[bold]Container Image[/bold]")
        # Skip image check when running inside a jail — the nested podman
        # won't have the image loaded (it's on the host's runtime).
        in_jail = os.environ.get("YOLO_VERSION") is not None
        if in_jail:
            ok("Inside jail — image check skipped (managed by host)")
        else:
            check_image = _jail_image(detected_runtime)
            try:
                if detected_runtime == "container":
                    result = subprocess.run(
                        ["container", "image", "inspect", check_image],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0:
                        ok(f"Image loaded: {check_image}")
                    else:
                        warn(
                            f"Image '{check_image}' not loaded",
                            "Run 'yolo' once to build and load the image",
                        )
                else:
                    result = subprocess.run(
                        [
                            detected_runtime,
                            "images",
                            check_image,
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
                            f"Image '{check_image}' not loaded",
                            "Run 'yolo' once to build and load the image",
                        )
            except Exception as e:
                warn(f"Could not check image: {e}")
        console.print()

        console.print("[bold]Running Jails[/bold]")
        try:
            if detected_runtime == "container":
                result = subprocess.run(
                    ["container", "ls", "--filter", "name=yolo-"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # Parse Apple container ls table output
                containers = []
                for line in result.stdout.strip().splitlines()[1:]:  # skip header
                    parts = line.split()
                    if parts:
                        cname = parts[0]
                        if cname.startswith("yolo-"):
                            containers.append(f"{cname}\t")
            else:
                result = subprocess.run(
                    [
                        detected_runtime,
                        "ps",
                        "--filter",
                        "name=^yolo-",
                        "--format",
                        "{{.Names}}\t{{.RunningFor}}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                containers = [c for c in result.stdout.strip().split("\n") if c]
            if containers:
                orphaned_jails = []
                ok(f"{len(containers)} jail(s) running")
                for line in containers:
                    parts = line.split("\t")
                    cname = parts[0]
                    running_for = parts[1] if len(parts) > 1 else ""
                    workspace = _get_container_workspace(cname, detected_runtime)
                    ws_exists = (
                        Path(workspace).is_dir() if workspace != "unknown" else True
                    )
                    reason = None
                    if not ws_exists:
                        reason = "workspace gone"
                    else:
                        reason = _check_container_stuck(cname, detected_runtime)
                    if reason:
                        marker = f" [red]({reason})[/red]"
                        orphaned_jails.append((cname, running_for, workspace, reason))
                    else:
                        marker = ""
                    console.print(f"    {cname} → {workspace}{marker}")
                if orphaned_jails:
                    warn(
                        f"{len(orphaned_jails)} orphaned jail(s)",
                        "These containers are stuck or have lost their workspace",
                    )
                    console.print()
                    answer = console.input(
                        f"  [bold yellow]Stop {len(orphaned_jails)} orphaned jail(s)? [y/N][/bold yellow] "
                    )
                    if answer.strip().lower() in ("y", "yes"):
                        for cname, _, _, _ in orphaned_jails:
                            subprocess.run(
                                [detected_runtime, "rm", "-f", cname],
                                capture_output=True,
                            )
                            cleanup_container_tracking(cname)
                            console.print(f"    [green]Stopped {cname}[/green]")
            else:
                ok("No jails currently running")
        except Exception:
            warn("Could not check running containers")
        console.print()

    # --- Summary ---

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
    config_errors, config_warnings = _validate_config(config, workspace=workspace)
    # Cross-hierarchy overrides are valid, but same-file contradictions are not.
    try:
        user_raw = _load_jsonc_file(
            USER_CONFIG_PATH, str(USER_CONFIG_PATH), strict=False
        )
    except Exception:
        user_raw = {}
    ws_config_path = workspace / "yolo-jail.jsonc"
    try:
        ws_raw = _load_jsonc_file(ws_config_path, "yolo-jail.jsonc", strict=False)
    except Exception:
        ws_raw = {}
    config_errors.extend(_check_preset_null_conflicts(user_raw, str(USER_CONFIG_PATH)))
    config_errors.extend(_check_preset_null_conflicts(ws_raw, "yolo-jail.jsonc"))
    if config_warnings:
        for message in config_warnings:
            console.print(f"  [yellow]⚠ {message}[/yellow]")
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
        # Claude YOLO mode: settings.json already grants full permissions via
        # the "allow" list.  Do NOT inject --dangerously-skip-permissions — it
        # shows its own confirmation prompt and refuses to run as UID 0.
        # (IS_SANDBOX=1 bypasses the root check, but the prompt is still annoying.)
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
        _print_startup_banner(_get_yolo_version(), runtime, cname)
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
            _print_startup_banner(_get_yolo_version(), runtime, cname)
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

    # Remove any stopped container with the same name left over from an
    # unclean shutdown (e.g. OOM-kill, host reboot).  Without this,
    # `docker run --name <cname>` fails with "container already exists".
    stale_cid = find_existing_container(cname, runtime=runtime)
    if stale_cid:
        print(f"Removing stale container {cname}...", file=sys.stderr)
        _remove_stale_container(cname, runtime=runtime)

    import time as _time

    _profile_times = {}
    if profile:
        _profile_times["start"] = _time.monotonic()

    extra_packages = config.get("packages", [])
    mise_tools = _merge_mise_tools(config)
    lsp_servers = config.get("lsp_servers", {})
    mcp_servers = config.get("mcp_servers", {})
    mcp_presets = config.get("mcp_presets", [])
    host_claude_files = config.get("host_claude_files", DEFAULT_HOST_CLAUDE_FILES)
    user_env = config.get("env", {})
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
    docker_flags = [
        "--rm",
        "-i",
        "--init",
        "--read-only",
        "--name",
        cname,
    ]
    # Apple Container doesn't support --cgroupns
    if runtime != "container":
        docker_flags.insert(3, "--cgroupns=private")
    if runtime == "podman":
        # Podman auto-adds tmpfs mounts for /run, /tmp, /dev/shm when --read-only
        # is set.  This conflicts with our explicit --tmpfs /tmp and can trigger
        # conmon JSON parsing errors with crun.  Disable the auto-tmpfs and let
        # our explicit mounts handle it.
        docker_flags.append("--read-only-tmpfs=false")
    if sys.stdout.isatty():
        docker_flags.append("-t")

    # Per-workspace overlays for workspace-specific state
    ws_state = workspace / ".yolo" / "home"
    ws_state.mkdir(parents=True, exist_ok=True)
    (ws_state / "ssh").mkdir(exist_ok=True, mode=0o700)
    # Per-workspace writable overlays — isolate cross-jail writes.
    # These sit on top of the :ro GLOBAL_HOME base so each jail has its
    # own copy of generated configs, installed tools, and caches.
    for subdir in [
        "npm-global",
        "local",
        "go",
        "yolo-shims",
        "config",
        "copilot",
        "gemini",
        "claude",
    ]:
        (ws_state / subdir).mkdir(exist_ok=True)
    for fname in [
        "bash_history",
        "yolo-bootstrap.sh",
        "yolo-venv-precreate.sh",
        "yolo-perf.log",
        "yolo-socat.log",
        "yolo-entrypoint.lock",
    ]:
        (ws_state / fname).touch()

    # Seed agent config dirs with auth tokens from the :ro GLOBAL_HOME base.
    # On first boot for this workspace the per-workspace dirs are empty — copy
    # auth-related files so agents can authenticate.  Subsequent boots skip
    # files that already exist (the entrypoint regenerates configs each time).
    _seed_agent_dir(GLOBAL_HOME / ".copilot", ws_state / "copilot")
    _seed_agent_dir(GLOBAL_HOME / ".gemini", ws_state / "gemini")
    _seed_agent_dir(GLOBAL_HOME / ".claude", ws_state / "claude")

    # Seed claude.json onboarding state into the per-workspace overlay.
    # ~/.claude.json is a symlink → .claude/claude.json, so the actual file
    # lives inside the writable .claude/ overlay.  Merge GLOBAL_HOME's data
    # (hasCompletedOnboarding, numStartups, oauthAccount, etc.) into the
    # per-workspace file, filling missing keys while preserving workspace-specific
    # MCP server config.
    src_claude_json = GLOBAL_HOME / ".claude" / "claude.json"
    dst_claude_json = ws_state / "claude" / "claude.json"
    if src_claude_json.is_file():
        try:
            src_data = json.loads(src_claude_json.read_text())
            try:
                dst_data = json.loads(dst_claude_json.read_text())
            except (json.JSONDecodeError, FileNotFoundError, OSError):
                dst_data = {}
            for key, val in src_data.items():
                if key not in dst_data:
                    dst_data[key] = val
            dst_claude_json.write_text(json.dumps(dst_data, indent=2) + "\n")
        except (json.JSONDecodeError, OSError):
            pass

    # Migrate old per-workspace overlays into new unified agent dirs.
    # Before the read-only refactor, agent state used individual file/dir overlays
    # (e.g. claude-projects/, copilot-sessions/).  Now each agent gets a single
    # dir overlay (claude/, copilot/, gemini/).  Copy old data once if present.
    _migrate_old_overlay(ws_state / "claude-projects", ws_state / "claude" / "projects")
    _migrate_old_overlay(
        ws_state / "copilot-sessions", ws_state / "copilot" / "session-state"
    )
    _migrate_old_overlay(ws_state / "gemini-history", ws_state / "gemini" / "history")

    # Migrate old claude-settings.json file overlay into new claude/settings.json.
    # Preserves user customizations (model, hooks, etc.) from pre-refactor.
    old_claude_settings = ws_state / "claude-settings.json"
    new_claude_settings = ws_state / "claude" / "settings.json"
    if old_claude_settings.is_file() and not new_claude_settings.exists():
        (ws_state / "claude").mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_claude_settings, new_claude_settings)

    if runtime == "container":
        # Apple Container has a limit of ~22 directory sharing devices
        # (Virtualization.framework constraint).  Instead of the GLOBAL_HOME :ro
        # base + 15 individual per-workspace writable overlays (which would use
        # 16 slots), mount ws_state as a single writable /home/agent.
        # Auth tokens are already seeded into ws_state from GLOBAL_HOME above.
        docker_cmd = [
            runtime,
            "run",
            *docker_flags,
            "-v",
            f"{workspace}:/workspace",
            "-v",
            f"{ws_state}:/home/agent",
            "-v",
            f"{GLOBAL_CACHE}:/home/agent/.cache",
            "-v",
            "yolo-mise-data:/mise",
            # Apple Container's --tmpfs only takes a plain path (no options)
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/var/tmp",
            "--tmpfs",
            "/var/lib/containers",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/dev/shm",
        ]
    else:
        docker_cmd = [
            runtime,
            "run",
            *docker_flags,
            "-v",
            f"{workspace}:/workspace",
            # Global home — read-only base with auth tokens and base configs.
            # Per-workspace writable overlays are mounted on top below.
            "-v",
            f"{GLOBAL_HOME}:/home/agent:ro",
            # --- Per-workspace writable overlays (isolate cross-jail writes) ---
            # Directories: installed tools, generated configs, shims
            "-v",
            f"{ws_state / 'npm-global'}:/home/agent/.npm-global",
            "-v",
            f"{ws_state / 'local'}:/home/agent/.local",
            "-v",
            f"{ws_state / 'go'}:/home/agent/go",
            "-v",
            f"{ws_state / 'yolo-shims'}:/home/agent/.yolo-shims",
            "-v",
            f"{ws_state / 'config'}:/home/agent/.config",
            # Shared download cache (CAS — safe across workspaces, avoids re-downloads)
            "-v",
            f"{GLOBAL_CACHE}:/home/agent/.cache",
            # Files: generated scripts, configs, logs
            # (.bashrc and .gitconfig are symlinks into the writable .config/ overlay,
            # so they don't need separate file bind mounts.)
            "-v",
            f"{ws_state / 'yolo-bootstrap.sh'}:/home/agent/.yolo-bootstrap.sh",
            "-v",
            f"{ws_state / 'yolo-venv-precreate.sh'}:/home/agent/.yolo-venv-precreate.sh",
            "-v",
            f"{ws_state / 'yolo-perf.log'}:/home/agent/.yolo-perf.log",
            "-v",
            f"{ws_state / 'yolo-socat.log'}:/home/agent/.yolo-socat.log",
            "-v",
            f"{ws_state / 'yolo-entrypoint.lock'}:/home/agent/.yolo-entrypoint.lock",
            # Agent config dirs — full per-workspace overlays.
            # Auth tokens are seeded from GLOBAL_HOME on first use (see _seed_agent_dir).
            # The entrypoint regenerates all configs into these writable dirs each boot.
            "-v",
            f"{ws_state / 'copilot'}:/home/agent/.copilot",
            "-v",
            f"{ws_state / 'gemini'}:/home/agent/.gemini",
            "-v",
            f"{ws_state / 'claude'}:/home/agent/.claude",
            # Other per-workspace overlays
            "-v",
            f"{ws_state / 'bash_history'}:/home/agent/.bash_history",
            "-v",
            f"{ws_state / 'ssh'}:/home/agent/.ssh",
            # --- Shared mounts ---
            "-v",
            # On macOS the host mise dir has Mach-O (darwin) binaries that cannot
            # execute inside the Linux container.  Use a Docker named volume so the
            # container installs its own native Linux toolchains.  The volume
            # persists across runs so subsequent starts are fast.
            "yolo-mise-data:/mise" if IS_MACOS else f"{host_mise}:/mise",
            "--tmpfs",
            # Explicit mode=1777 ensures non-root UIDs can write to tmpfs
            # (Docker on some backends defaults to 755).
            "/tmp:exec,mode=1777",
            "--tmpfs",
            "/var/tmp:exec,mode=1777",
            # Podman needs writable storage, runtime dirs, and shared memory for nested containers.
            # --read-only-tmpfs=false disables automatic tmpfs mounts (including /dev/shm),
            # so we must explicitly mount all tmpfs paths podman needs.
            "--tmpfs",
            "/var/lib/containers",
            "--tmpfs",
            "/run",
            "--tmpfs",
            "/dev/shm:size=2g",
        ]

    # Common env vars and flags for all runtimes
    docker_cmd.extend(
        [
            "-e",
            "JAIL_HOME=/home/agent",
            "-e",
            "NPM_CONFIG_PREFIX=/home/agent/.npm-global",
            "-e",
            # Redirect npm cache to the writable shared cache dir (GLOBAL_HOME is :ro,
            # so the default ~/.npm/_cacache would fail with EROFS).
            "NPM_CONFIG_CACHE=/home/agent/.cache/npm",
            "-e",
            "GOPATH=/home/agent/go",
            "-e",
            "MISE_DATA_DIR=/mise",
            "-e",
            # Use a per-container cache dir so mise lockfiles don't contend with
            # the host/outer-jail's locks (shared /home/agent would otherwise share
            # ~/.cache/mise/lockfiles/, causing deadlocks in nested jails).
            "MISE_CACHE_DIR=/tmp/mise-cache",
            "-e",
            # Explicitly request the non-freethreaded prebuilt to avoid
            # "missing lib directory" errors from freethreaded builds.
            "MISE_PYTHON_PRECOMPILED_FLAVOR=install_only_stripped",
            "-e",
            "MISE_TRUST=1",
            "-e",
            "MISE_YES=1",
            "-e",
            "COPILOT_ALLOW_ALL=true",
            # Tell Claude Code this is a sandboxed environment so it skips the
            # root-user check that blocks bypassPermissions / --dangerously-skip-permissions.
            # This is a belt-and-suspenders fix: the entrypoint also configures
            # permissions.allow rules instead of bypassPermissions.
            "-e",
            "IS_SANDBOX=1",
            "-e",
            f"LD_LIBRARY_PATH=/lib:/usr/lib:/usr/lib/{_linux_multilib()}",
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
            f"YOLO_VERSION={_git_describe_version() or 'unknown'}",
            "-e",
            "OVERMIND_SOCKET=/tmp/overmind.sock",
            "-e",
            f"YOLO_MISE_TOOLS={json.dumps(mise_tools)}",
            "-e",
            f"YOLO_LSP_SERVERS={json.dumps(lsp_servers)}",
            "-e",
            f"YOLO_MCP_SERVERS={json.dumps(mcp_servers)}",
            "-e",
            f"YOLO_MCP_PRESETS={json.dumps(mcp_presets)}",
            "-e",
            # Inside the container, podman is always the available runtime (it's
            # built into the image).  Using the host's runtime value (e.g. docker
            # on macOS) would fail since docker CLI isn't in the container.
            "YOLO_RUNTIME=podman",
            "-e",
            "YOLO_REPO_ROOT=/opt/yolo-jail",
        ]
    )

    # User-defined environment variables from config
    for env_key, env_val in user_env.items():
        docker_cmd.extend(["-e", f"{env_key}={env_val}"])

    docker_cmd += [
        "--workdir",
        "/workspace",
        # Mount yolo-jail repo for in-jail CLI (yolo --help, nested jailing).
        # In nested jails, YOLO_REPO_ROOT may point to an empty /opt/yolo-jail
        # (bind mount doesn't propagate). Fall back to /workspace if it's the repo.
        "-v",
        f"{repo_root}:/opt/yolo-jail:ro"
        if (repo_root / "flake.nix").exists()
        else f"{workspace}:/opt/yolo-jail:ro",
    ]

    # Docker needs explicit UID mapping; podman rootless maps container root to host user.
    # On macOS, Docker runs inside a VM — the host UID (e.g. 501) doesn't exist
    # in the container, causing permission errors on volumes.  Skip -u on macOS
    # and let the container run as root (its default/intended user).
    # Apple Container: each container is its own VM — no UID mapping needed.
    if runtime == "docker" and not IS_MACOS:
        docker_cmd.extend(["-u", f"{os.getuid()}:{os.getgid()}"])

    # Detect if we're already inside a container (macOS host is never in a container)
    in_container = not IS_MACOS and (
        Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()
    )

    # Check if GPU passthrough is enabled (affects user namespace strategy)
    gpu_enabled = config.get("gpu", {}).get("enabled", False)

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
        elif gpu_enabled:
            # GPU passthrough: CDI device injection fails with crun and custom
            # user namespaces (https://github.com/containers/podman/issues/27483).
            # Use runc to avoid the CDI+crun incompatibility, and identity UID/GID
            # mapping (same as non-GPU) instead of keep-id. keep-id forces podman
            # to shift UIDs across every file in every image layer — with a large
            # image (100 layers, multi-GB) and no native shifting support this
            # causes 10+ minute container startup. Identity mapping needs no
            # shifting since container UIDs match the namespace UIDs as stored.
            # SYS_ADMIN is needed for nested containers (podman-in-podman).
            docker_cmd.extend(
                [
                    "--security-opt",
                    "label=disable",
                    "--uidmap",
                    "0:0:1",
                    "--uidmap",
                    "1:1:65536",
                    "--gidmap",
                    "0:0:1",
                    "--gidmap",
                    "1:1:65536",
                    "--runtime",
                    "runc",
                    "--cap-add",
                    "SYS_ADMIN",
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
    # On macOS, /nix exists on the host but the container runtime's VM may not
    # have it mounted.  Podman Machine needs `--volume /nix:/nix` at init time;
    # Docker Desktop needs /nix added to file sharing settings.
    nix_socket = Path("/nix/var/nix/daemon-socket")
    nix_store = Path("/nix/store")
    if nix_socket.exists() and nix_store.exists() and runtime != "container":
        # Apple Container VMs can't share Unix sockets via -v bind mounts
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
    # Apple Container: each container gets its own VM with dedicated networking;
    # --net flags are not supported.
    if runtime == "container":
        pass  # Apple Container handles networking internally
    elif runtime == "podman" and in_container:
        docker_cmd.append("--net=host")
    elif net_mode != "bridge" or runtime == "docker":
        docker_cmd.append(f"--net={net_mode}")

    # Docker bridge: add host.internal → host-gateway so socat (and agents)
    # can reach host services.  Podman does this automatically.
    # Apple Container: containers have their own IP and can reach the host directly.
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

    # Enable iptables DNAT so published ports reach services bound to 127.0.0.1.
    # Container runtimes forward published-port traffic to the container's eth0,
    # not loopback — so services listening on localhost never see it.
    # Podman rootless runs as UID 0 in a user namespace, so iptables works.
    # Docker uses -u UID:GID (non-root, no NET_ADMIN) so this is Podman-only.
    # route_localnet allows the kernel to route DNAT'd packets to 127.0.0.1;
    # the entrypoint adds matching iptables PREROUTING rules.
    if publish_args and runtime == "podman":
        docker_cmd.extend(["--sysctl", "net.ipv4.conf.all.route_localnet=1"])
        # Extract container-side ports for the entrypoint's DNAT rules
        published_ports = []
        for p in config.get("network", {}).get("ports", []):
            spec = str(p)
            proto = "tcp"
            if "/" in spec:
                spec, proto = spec.rsplit("/", 1)
            parts = spec.split(":")
            container_port = parts[-1]  # always the last element
            published_ports.append(f"{container_port}/{proto}")
        if published_ports:
            docker_cmd.extend(
                ["-e", f"YOLO_PUBLISHED_PORTS={json.dumps(published_ports)}"]
            )

    # Host port forwarding.
    # On Linux: uses Unix sockets bind-mounted between host and container.
    # On macOS+Docker: virtiofs doesn't support Unix sockets, so the container-side
    # socat connects directly to host.docker.internal (TCP) instead.
    # On macOS+Apple Container: native --publish-socket for socket forwarding.
    _host_tmp = Path("/tmp").resolve() if IS_MACOS else Path("/tmp")
    socket_dir = None
    if forward_host_ports:
        docker_cmd.extend(
            ["-e", f"YOLO_FORWARD_HOST_PORTS={json.dumps(forward_host_ports)}"]
        )
        if runtime == "container":
            # Apple Container: native socket forwarding (no TCP gateway needed)
            socket_dir = _host_tmp / f"yolo-fwd-{cname}"
            socket_dir.mkdir(parents=True, exist_ok=True)
            for port_spec in forward_host_ports:
                port = str(port_spec).split(":")[0]
                host_sock = socket_dir / f"port-{port}.sock"
                docker_cmd.extend(
                    [
                        "--publish-socket",
                        f"{host_sock}:/tmp/yolo-fwd/port-{port}.sock",
                    ]
                )
        elif IS_MACOS:
            # Tell the container entrypoint to use TCP forwarding via the
            # Docker host gateway instead of Unix sockets.
            docker_cmd.extend(["-e", "YOLO_FWD_HOST_GATEWAY=host.docker.internal"])
        else:
            socket_dir = _host_tmp / f"yolo-fwd-{cname}"
            docker_cmd.extend(["-v", f"{socket_dir}:/tmp/yolo-fwd:rw"])

    # Host-side cgroup delegate: Unix socket for safe cgroup operations
    # Apple Container doesn't support cgroup delegation or Unix socket bind mounts
    cgd_socket_dir = _host_tmp / f"yolo-cgd-{cname}"
    if runtime != "container":
        docker_cmd.extend(["-v", f"{cgd_socket_dir}:/tmp/yolo-cgd:rw"])

    # Device passthrough from config
    # On macOS, device passthrough goes through the container runtime's VM.
    # Raw /dev paths and lsusb are Linux concepts — USB passthrough is not
    # supported on macOS.  Device cgroup rules are also Linux-only.
    for dev in config.get("devices", []):
        if isinstance(dev, str):
            # Raw device path: "/dev/bus/usb/001/004"
            if IS_MACOS:
                console.print(
                    f"[yellow]Warning: device passthrough ({dev}) not supported on macOS — skipping[/yellow]"
                )
                continue
            if not Path(dev).exists():
                console.print(
                    f"[yellow]Warning: device {dev} not found — skipping[/yellow]"
                )
                continue
            docker_cmd.extend(["--device", dev])
        elif isinstance(dev, dict):
            if "usb" in dev:
                usb_id = dev["usb"]
                desc = dev.get("description", usb_id)
                if IS_MACOS:
                    console.print(
                        f"[yellow]Warning: USB device passthrough ({desc}) not supported on macOS — skipping[/yellow]"
                    )
                    continue
                # Resolve USB vendor:product ID to /dev/bus/usb path
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
                if IS_MACOS:
                    console.print(
                        "[yellow]Warning: device cgroup rules not supported on macOS — skipping[/yellow]"
                    )
                    continue
                docker_cmd.extend(["--device-cgroup-rule", dev["cgroup_rule"]])

    # GPU passthrough from config (NVIDIA only, not available on macOS)
    gpu_config = config.get("gpu", {})
    if gpu_config.get("enabled", False):
        if IS_MACOS:
            console.print(
                "[yellow]Warning: GPU passthrough is not supported on macOS — skipping[/yellow]"
            )
        else:
            gpu_devices = gpu_config.get("devices", "all")
            gpu_capabilities = gpu_config.get("capabilities", "compute,utility")

            if runtime == "docker":
                # Docker: use --gpus flag (requires nvidia-container-toolkit)
                if gpu_devices == "all":
                    docker_cmd.extend(["--gpus", "all"])
                else:
                    docker_cmd.extend(["--gpus", f'"device={gpu_devices}"'])
            elif runtime == "podman":
                # Podman: use CDI (Container Device Interface) notation
                if gpu_devices == "all":
                    docker_cmd.extend(["--device", "nvidia.com/gpu=all"])
                else:
                    # CDI supports individual GPU indices: nvidia.com/gpu=0
                    for gpu_idx in gpu_devices.split(","):
                        gpu_idx = gpu_idx.strip()
                        docker_cmd.extend(["--device", f"nvidia.com/gpu={gpu_idx}"])

            # Set NVIDIA environment variables for the container runtime to pick up
            docker_cmd.extend(
                [
                    "-e",
                    f"NVIDIA_VISIBLE_DEVICES={gpu_devices}",
                    "-e",
                    f"NVIDIA_DRIVER_CAPABILITIES={gpu_capabilities}",
                ]
            )
            console.print(
                f"[dim]GPU passthrough: devices={gpu_devices}, capabilities={gpu_capabilities}[/dim]"
            )

    # Resource limits from config.
    # Apple Container needs explicit defaults (its built-in defaults are 4 CPU / 1GB RAM).
    # Docker/Podman inherit VM-level resources; only set limits when explicitly configured.
    resources_config = config.get("resources", {})
    res_parts = []
    memory = resources_config.get("memory")
    cpus = resources_config.get("cpus")

    if runtime == "container":
        # Apple Container: apply sane defaults since its built-ins are too low
        # for agent workloads. Default to half the host resources.
        if cpus is None:
            import multiprocessing

            host_cpus = multiprocessing.cpu_count()
            cpus = max(2, host_cpus // 2)
        if memory is None:
            try:
                if IS_MACOS:
                    result = subprocess.run(
                        ["sysctl", "-n", "hw.memsize"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    host_mem_bytes = int(result.stdout.strip())
                else:
                    host_mem_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf(
                        "SC_PHYS_PAGES"
                    )
                # Default to half of host memory, minimum 4GB, formatted for Apple Container
                default_mem = max(4 * 1024**3, host_mem_bytes // 2)
                memory = f"{default_mem // (1024**3)}g"
            except Exception:
                memory = "8g"

    if memory:
        docker_cmd.extend(["--memory", memory])
        res_parts.append(f"memory={memory}")
    if cpus is not None:
        docker_cmd.extend(["--cpus", str(cpus)])
        res_parts.append(f"cpus={cpus}")
    pids_limit = resources_config.get("pids_limit")
    # Apple Container doesn't support --pids-limit (each container is a VM)
    if runtime != "container":
        # Podman defaults to 2048 pids which is too low for agent workloads.
        # Always set a sane default.
        effective_pids = pids_limit if pids_limit is not None else 32768
        docker_cmd.extend(["--pids-limit", str(effective_pids)])
        res_parts.append(f"pids={effective_pids}")
    # Print version at startup for log capture
    _print_startup_banner(_get_yolo_version(), runtime, cname, res_parts or None)

    # Mount host nvim config read-only for entrypoint to copy into the writable
    # .config/ overlay.  We can't bind-mount directly because dotfile managers
    # (stow, etc.) create symlinks that break inside the container.
    host_nvim_config = Path.home() / ".config" / "nvim"
    if host_nvim_config.is_dir():
        docker_cmd.extend(["-v", f"{host_nvim_config}:/ctx/host-nvim-config:ro"])

    # Shadow workspace .vscode/mcp.json so agents use only our jail MCP config
    vscode_mcp = workspace / ".vscode" / "mcp.json"
    if vscode_mcp.exists():
        docker_cmd.extend(["-v", "/dev/null:/workspace/.vscode/mcp.json:ro"])

    # Shadow workspace .overmind.sock so host overmind doesn't leak into the jail
    overmind_sock = workspace / ".overmind.sock"
    if overmind_sock.exists():
        docker_cmd.extend(["-v", "/dev/null:/workspace/.overmind.sock:ro"])

    # Mount user-level yolo config so nested jails see the same merged config.
    # Without this, ~/.config/ is an empty per-workspace overlay and the nested
    # yolo resolves to empty config, stomping the host's config snapshot.
    if USER_CONFIG_PATH.is_file():
        container_config = f"/home/agent/.config/yolo-jail/{USER_CONFIG_PATH.name}"
        docker_cmd.extend(["-v", f"{USER_CONFIG_PATH}:{container_config}:ro"])

    # Pass container-side mise path for nested jail re-mounting.
    # Inside the container, mise is always at /mise regardless of host path.
    docker_cmd.extend(["-e", "YOLO_OUTER_MISE_PATH=/mise"])

    # Mount merged skills directories read-only (prepared on host side).
    # Kernel-enforced :ro — agents get "Read-only file system" on write attempts.
    skills_path = _prepare_skills(cname, workspace)
    docker_cmd.extend(
        ["-v", f"{skills_path / 'skills-copilot'}:/home/agent/.copilot/skills:ro"]
    )
    docker_cmd.extend(
        ["-v", f"{skills_path / 'skills-gemini'}:/home/agent/.gemini/skills:ro"]
    )
    docker_cmd.extend(
        ["-v", f"{skills_path / 'skills-claude'}:/home/agent/.claude/skills:ro"]
    )

    # Mount host ~/.claude/ files for syncing into the jail.
    # Auto-discover scripts referenced in host settings.json (fileSuggestion,
    # statusLine, hooks) and include them if they live under ~/.claude/.
    host_claude_dir = Path.home() / ".claude"
    effective_claude_files = list(host_claude_files)
    host_settings_file = host_claude_dir / "settings.json"
    if host_settings_file.exists():
        try:
            host_settings = json.loads(host_settings_file.read_text())
            # Collect all command paths referenced in settings
            script_cmds: List[str] = []
            for key in ("fileSuggestion", "statusLine"):
                cmd = (host_settings.get(key) or {}).get("command", "")
                if cmd:
                    script_cmds.append(cmd)
            # Walk hooks: {"EventName": [{"hooks": [{"command": "..."}]}]}
            for _event, matchers in (host_settings.get("hooks") or {}).items():
                if not isinstance(matchers, list):
                    continue
                for matcher in matchers:
                    for hook in matcher.get("hooks") or []:
                        cmd = hook.get("command", "") if isinstance(hook, dict) else ""
                        if cmd:
                            script_cmds.append(cmd)
            # Add scripts that live under ~/.claude/
            for cmd in script_cmds:
                resolved = Path(cmd.replace("~", str(Path.home())))
                try:
                    resolved.relative_to(host_claude_dir)
                    fname = resolved.name
                    if fname not in effective_claude_files:
                        effective_claude_files.append(fname)
                except ValueError:
                    pass  # script lives outside ~/.claude/, must mount manually
        except (json.JSONDecodeError, OSError):
            pass
    mounted_claude_files = []
    for fname in effective_claude_files:
        host_file = host_claude_dir / fname
        if host_file.exists() and host_file.is_file():
            docker_cmd.extend(["-v", f"{host_file}:/ctx/host-claude/{fname}:ro"])
            mounted_claude_files.append(fname)
    if mounted_claude_files:
        docker_cmd.extend(
            ["-e", f"YOLO_HOST_CLAUDE_FILES={json.dumps(mounted_claude_files)}"]
        )

    # Generate per-workspace AGENTS.md / CLAUDE.md (separate for each agent to
    # respect user-level ~/.copilot/AGENTS.md, ~/.gemini/AGENTS.md, ~/.claude/CLAUDE.md)
    agents_path = generate_agents_md(
        cname,
        workspace,
        normalized_blocked,
        mount_descriptions,
        net_mode=net_mode,
        runtime=runtime,
        forward_host_ports=forward_host_ports or None,
        mcp_servers=mcp_servers or None,
        mcp_presets=mcp_presets or None,
    )
    docker_cmd.extend(
        ["-v", f"{agents_path / 'AGENTS-copilot.md'}:/home/agent/.copilot/AGENTS.md:ro"]
    )
    docker_cmd.extend(
        ["-v", f"{agents_path / 'AGENTS-gemini.md'}:/home/agent/.gemini/AGENTS.md:ro"]
    )
    docker_cmd.extend(
        ["-v", f"{agents_path / 'CLAUDE.md'}:/home/agent/.claude/CLAUDE.md:ro"]
    )

    if "TERM" in os.environ:
        docker_cmd.extend(["-e", f"TERM={os.environ['TERM']}"])

    if profile:
        docker_cmd.extend(["-e", "YOLO_PROFILE=1"])

    docker_cmd.append(_jail_image(runtime))
    docker_cmd.append("yolo-entrypoint")

    # If mise.toml exists in workspace, trust it.
    # Then ensure all tools (global + local) are ready.
    # --quiet on mise trust suppresses "No untrusted config files found" warning.
    # mise upgrade stderr is filtered to hide deprecation noise (@system warnings).
    setup_script = (
        "YOLO_BYPASS_SHIMS=1 sh -c '"
        "(if [ -f mise.toml ]; then mise trust --quiet 2>/dev/null; fi) && "
        'echo "  ↳ mise install" >&2 && '
        "mise install --quiet && "
        'echo "  ↳ mise upgrade" >&2 && '
        'mise upgrade --yes 2>&1 | grep -v "^mise WARN" | sed "s/^/    /" >&2 && '
        'echo "  ↳ bootstrap" >&2 && '
        "~/.yolo-bootstrap.sh >&2 && "
        "~/.yolo-venv-precreate.sh >&2'"
    )
    # After setup, activate mise so tool paths (copilot, gemini, claude, etc.) are in PATH.
    # We use `mise env` (one-time activation) rather than `mise hook-env` (continuous
    # shell integration) because hook-env deadlocks when it needs to create a venv:
    # it holds a lock, spawns `uv` via the mise shim (which IS mise), and the shim
    # tries to acquire the same lock → deadlock.
    # Re-prepend yolo-shims after mise env so our wrappers (yolo, blocked tools)
    # take priority over mise-installed console_scripts in installs/python/.../bin/.
    mise_activate = (
        'eval "$(mise env -s bash)" 2>/dev/null; export PATH="$HOME/.yolo-shims:$PATH"'
    )

    # Human-readable command for status messages
    display_cmd = target_cmd.replace("'", "'\\''")

    # Use && for fail-fast: if provisioning fails, don't proceed with broken env
    if profile:
        # Wrap each phase with timing output for profiling
        final_internal_cmd = (
            "exec 3>&2; "  # save stderr
            "printf '\\033[2m📦 Provisioning tools...\\033[0m\\n' >&2; "
            f"_t0=$(date +%s%N); {setup_script}; "
            "_t1=$(date +%s%N); "
            f"{mise_activate}; "
            "_t2=$(date +%s%N); "
            f"printf '\\033[1;36m⚡ Executing: {display_cmd}\\033[0m\\n' >&2; "
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
        # Provisioning message → bootstrap → activate → ready → command
        final_internal_cmd = (
            "printf '\\033[2m📦 Provisioning tools...\\033[0m\\n' >&2 && "
            f"{setup_script} && "
            f"{mise_activate}; "
            f"printf '\\033[1;36m⚡ Executing: {display_cmd}\\033[0m\\n' >&2; "
            f"{target_cmd}"
        )

    docker_cmd.append(final_internal_cmd)

    write_container_tracking(cname, workspace)
    _tmux_rename_window("JAIL")

    # Start host-side port forwarding BEFORE the container so socket files
    # exist when entrypoint.py starts the container-side socat.
    socat_procs: List[subprocess.Popen] = []
    if socket_dir:
        socat_procs = start_host_port_forwarding(forward_host_ports, cname, socket_dir)

    # Start host-side cgroup delegate daemon BEFORE the container so the
    # socket exists when the entrypoint or agent runs yolo-cglimit.
    cgd_thread = start_cgroup_delegate(cname, runtime, cgd_socket_dir)

    if os.environ.get("YOLO_DEBUG"):
        print(" ".join(shlex.quote(s) for s in docker_cmd), file=sys.stderr)

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
        stop_cgroup_delegate(cgd_thread, cgd_socket_dir)
        lock_file.close()
        sys.exit(1)
    for _ in range(20):
        if find_running_container(cname, runtime=runtime):
            break
        _time.sleep(0.25)
    lock_file.close()

    proc.wait()
    result = proc

    # Clean up host-side socat processes, cgroup delegate, and socket directories
    cleanup_port_forwarding(socat_procs, socket_dir)
    stop_cgroup_delegate(cgd_thread, cgd_socket_dir)

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


def _check_container_stuck(name: str, runtime: str) -> "str | None":
    """Check if a container is stuck in provisioning by inspecting its process tree.

    Returns a reason string if stuck, None if healthy.
    """
    if runtime == "container":
        # Apple Container CLI doesn't support 'top'
        return None
    try:
        result = subprocess.run(
            [runtime, "top", name, "-eo", "comm"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        procs = [p.strip() for p in result.stdout.strip().splitlines()[1:] if p.strip()]
        if not procs:
            return "no processes"
        # A healthy container has user commands running (claude, copilot, bash shell, etc.)
        # A stuck container's leaf processes are provisioning tools
        provisioning_commands = {"uv", "mise", "pip", "npm"}
        # Check if ALL non-init processes are provisioning-related
        user_procs = [
            p
            for p in procs
            if p not in provisioning_commands
            and p not in ("bash", "sh", "podman-init", "yolo-entrypo", "sleep", "sed")
        ]
        if not user_procs:
            return "stuck in provisioning"
    except Exception:
        pass
    return None


def _get_container_workspace(name: str, runtime: str) -> str:
    """Get the workspace path for a running container via inspect or tracking file."""
    # Try tracking file first (fast)
    tracking_file = CONTAINER_DIR / name
    if tracking_file.exists():
        ws = tracking_file.read_text().strip()
        if ws:
            return ws
    # Fall back to inspecting the container's YOLO_HOST_DIR env var
    try:
        if runtime == "container":
            # Apple Container: inspect outputs JSON without --format support
            result = subprocess.run(
                ["container", "inspect", name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    # Apple Container inspect returns a dict with config.env
                    env_list = data.get("config", {}).get("env", [])
                    for env_entry in env_list:
                        if env_entry.startswith("YOLO_HOST_DIR="):
                            return env_entry.split("=", 1)[1]
                except (ValueError, KeyError, TypeError):
                    pass
        else:
            result = subprocess.run(
                [
                    runtime,
                    "inspect",
                    name,
                    "--format",
                    "{{range .Config.Env}}{{println .}}{{end}}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("YOLO_HOST_DIR="):
                        return line.split("=", 1)[1]
    except Exception:
        pass
    return "unknown"


@app.command()
def ps():
    """List running YOLO jail containers."""
    runtime = _runtime()
    if runtime == "container":
        result = subprocess.run(
            ["container", "ls", "--filter", "name=yolo-"],
            capture_output=True,
            text=True,
        )
        lines = []
        for line in result.stdout.strip().splitlines()[1:]:  # skip header
            parts = line.split()
            if parts and parts[0].startswith("yolo-"):
                cname = parts[0]
                status = " ".join(parts[1:]) if len(parts) > 1 else ""
                lines.append(f"{cname}\t{status}\t")
    else:
        result = subprocess.run(
            [
                runtime,
                "ps",
                "--filter",
                "name=^yolo-",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.RunningFor}}",
            ],
            capture_output=True,
            text=True,
        )
        lines = result.stdout.strip().splitlines() if result.stdout.strip() else []

    if not lines:
        typer.echo("No running jails.")
        # Clean up all stale tracking files
        if CONTAINER_DIR.exists():
            for tracking_file in CONTAINER_DIR.iterdir():
                cleanup_container_tracking(tracking_file.name)
        return

    # Parse container info and resolve workspaces
    containers = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 3:
            name, status, running_for = parts[0], parts[1], parts[2]
            workspace = _get_container_workspace(name, runtime)
            containers.append((name, status, running_for, workspace))

    # Clean up stale tracking files
    running_names = {c[0] for c in containers}
    if CONTAINER_DIR.exists():
        for tracking_file in CONTAINER_DIR.iterdir():
            if tracking_file.name not in running_names:
                cleanup_container_tracking(tracking_file.name)

    # Display as a table
    if containers:
        w_name = max(len(c[0]) for c in containers)
        w_status = max(len(c[1]) for c in containers)
        header = f"{'CONTAINER':<{w_name}}  {'STATUS':<{w_status}}  WORKSPACE"
        typer.echo(header)
        for name, status, _running_for, workspace in containers:
            typer.echo(f"{name:<{w_name}}  {status:<{w_status}}  {workspace}")

    # Warn about orphaned/stuck jails
    problems = []
    for name, _status, _running_for, workspace in containers:
        if workspace != "unknown" and not Path(workspace).is_dir():
            problems.append((name, "workspace gone"))
        else:
            reason = _check_container_stuck(name, runtime)
            if reason:
                problems.append((name, reason))
    if problems:
        typer.echo(f"\n⚠  {len(problems)} problem jail(s):")
        for name, reason in problems:
            typer.echo(f"  {name}  ({reason})")
        typer.echo("\n  Run 'yolo doctor' to clean up")


@app.command()
def doctor(
    build: bool = typer.Option(
        True,
        "--build/--no-build",
        help="Run nix build as part of the preflight (default: on)",
    ),
):
    """Alias for 'check'. Validate environment, config, and build."""
    check(build=build)


def main():
    """Entry point for the `yolo` console script.

    Handles visual jail indicator (kitty tab or tmux pane border) and routes to
    the typer CLI.  Detection priority: kitty-native > tmux > neither.
    YOLO_NO_TMUX=1 skips all tmux interactions (useful in kitty-only setups).
    """
    import atexit

    # Rewrite argv so `yolo -- echo foo` routes to `yolo run -- echo foo`.
    # Typer groups resolve the first positional arg as a subcommand name, so
    # extra args after `--` that aren't subcommands would fail with "No such
    # command".  We detect this and insert `run` before `--`.
    _SUBCOMMANDS = {
        "init",
        "init-user-config",
        "config-ref",
        "check",
        "run",
        "ps",
        "doctor",
    }
    args = sys.argv[1:]
    if args and "--" in args:
        pre_dash = args[: args.index("--")]
        # If nothing before `--` looks like a subcommand, insert `run`
        if not any(a in _SUBCOMMANDS for a in pre_dash):
            idx = sys.argv.index("--")
            sys.argv.insert(idx, "run")

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
