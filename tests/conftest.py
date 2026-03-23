"""
Session-level fixture to ensure the yolo-jail image is loaded into the container
runtime before integration tests run. This is needed when pytest itself runs inside
a jail (nested-container scenario), where the inner podman has its own separate image
store that doesn't see the outer host's images.
"""

import subprocess
import shutil
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
JAIL_IMAGE = "yolo-jail:latest"


def _detect_runtime() -> str | None:
    for rt in ("podman", "docker"):
        if shutil.which(rt):
            return rt
    return None


def _image_exists(runtime: str) -> bool:
    result = subprocess.run(
        [runtime, "image", "inspect", JAIL_IMAGE],
        capture_output=True,
    )
    return result.returncode == 0


@pytest.fixture(scope="session", autouse=True)
def ensure_jail_image():
    """
    Before any test runs, ensure yolo-jail:latest is loaded into the local container
    runtime. On the host this is a no-op (cli.py handles it). Inside a jail the inner
    podman has an empty image store, so we build via the host nix daemon and load.
    """
    in_container = Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()
    if not in_container:
        return  # cli.py already handles this on the host

    runtime = _detect_runtime()
    if runtime is None:
        pytest.skip("No container runtime (podman/docker) found")

    if _image_exists(runtime):
        return  # Already loaded from a previous session (persistent home dir)

    print(
        f"\n[conftest] Loading {JAIL_IMAGE} into inner {runtime} (this may take a minute)..."
    )

    # Build via host nix daemon (NIX_REMOTE=daemon + /nix/var/nix/daemon-socket are
    # mounted into the jail by cli.py so nix can delegate builds to the host daemon).
    build = subprocess.run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "build",
            ".#dockerImage",
            "--impure",
            "--out-link",
            str(REPO_ROOT / ".run-result"),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
    )

    result_link = REPO_ROOT / ".run-result"
    if build.returncode != 0:
        pytest.fail(
            f"nix build failed inside jail — cannot load {JAIL_IMAGE}.\n"
            f"stderr: {build.stderr.decode()}\n"
            "Ensure the host nix daemon socket is mounted (/nix/var/nix/daemon-socket) "
            "and NIX_REMOTE=daemon is set."
        )

    # streamLayeredImage produces an executable script that outputs the image
    # tar to stdout — we must execute it and pipe to `runtime load`, not read
    # the script as a file.  This matches the streaming pipeline in cli.py.
    resolved = str(result_link.resolve())
    try:
        stream_proc = subprocess.Popen(
            [resolved],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        load = subprocess.run(
            [runtime, "load"],
            stdin=stream_proc.stdout,
            capture_output=True,
        )
        stream_proc.wait()
        if stream_proc.returncode != 0 or load.returncode != 0:
            # Warn but don't fail — unit tests don't need the image
            import warnings

            warnings.warn(
                f"{runtime} load failed (integration tests may be skipped): "
                f"{load.stderr.decode().strip()}"
            )
            return
        print(f"[conftest] {load.stdout.decode().strip()}")
    finally:
        result_link.unlink(missing_ok=True)
