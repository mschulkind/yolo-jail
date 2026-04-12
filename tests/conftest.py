"""
Session-level fixture to ensure the yolo-jail image is loaded into the container
runtime before integration tests run. This is needed when pytest itself runs inside
a jail (nested-container scenario), where the inner podman has its own separate image
store that doesn't see the outer host's images.
"""

import subprocess
import shutil
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
JAIL_IMAGE = "yolo-jail:latest"


@pytest.fixture(autouse=True)
def _simulate_linux_for_unit_tests(request, monkeypatch):
    """Ensure *unit* tests exercise the Linux code paths regardless of host OS.

    Integration tests (marked ``@pytest.mark.slow``) are left untouched so they
    run with the real platform flags and whatever ``YOLO_RUNTIME`` the caller
    set in the environment.

    The CLI's IS_MACOS / IS_LINUX guards change runtime behaviour.  Unit tests
    are heavily mocked and should test the primary (Linux) code path.  Tests
    that specifically target macOS behaviour can override this with::

        monkeypatch.setattr("cli.IS_MACOS", True)
        monkeypatch.setattr("cli.IS_LINUX", False)

    Also clears YOLO_RUNTIME so the mocked tests use their own runtime
    detection rather than inheriting an env var from the test runner.
    """
    is_integration = any(m.name == "slow" for m in request.node.iter_markers())
    if is_integration:
        return  # let integration tests use real platform values

    monkeypatch.delenv("YOLO_RUNTIME", raising=False)
    if sys.platform == "darwin":
        # Lazily import — cli may not be on sys.path yet for conftest itself
        try:
            import cli as _cli

            monkeypatch.setattr(_cli, "IS_MACOS", False)
            monkeypatch.setattr(_cli, "IS_LINUX", True)
        except ImportError:
            pass


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
def _ensure_nix_in_path():
    """On macOS, ensure /nix/var/nix/profiles/default/bin is in PATH for
    test subprocesses that invoke cli.py (which calls ``nix build``)."""
    import os

    nix_bin = "/nix/var/nix/profiles/default/bin"
    if sys.platform == "darwin" and nix_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = nix_bin + ":" + os.environ.get("PATH", "")


@pytest.fixture(scope="session", autouse=True)
def ensure_jail_image():
    """
    Before any test runs, ensure yolo-jail:latest is loaded into the local container
    runtime. On the host this is a no-op (cli.py handles it). Inside a jail the inner
    podman has an empty image store, so we build via the host nix daemon and load.
    """
    in_container = sys.platform != "darwin" and (
        Path("/run/.containerenv").exists() or Path("/.dockerenv").exists()
    )
    if not in_container:
        return  # cli.py already handles this on the host

    runtime = _detect_runtime()
    if runtime is None:
        pytest.skip("No container runtime (podman/docker) found")

    if _image_exists(runtime):
        return  # Already loaded from a previous session (persistent home dir)

    # With --read-only root, podman storage is on a read-only filesystem and
    # cannot load new images.  Skip gracefully — unit tests don't need the image.
    storage_check = subprocess.run(
        [runtime, "info", "--format", "{{.Store.GraphRoot}}"],
        capture_output=True,
        timeout=10,
    )
    if storage_check.returncode != 0:
        import warnings

        warnings.warn(
            "Container runtime storage unavailable (read-only filesystem?) — "
            "integration tests may be skipped"
        )
        return

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
