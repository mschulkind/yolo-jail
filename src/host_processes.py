#!/usr/bin/env python3
"""yolo-jail host-processes loophole — allowlisted view of host processes.

The daemon runs on the host, listens on a Unix socket, and answers
``ps``-style requests against an allowlist configured in the user's
``yolo-jail.jsonc``:

.. code-block:: jsonc

    "host_processes": {
      "visible": ["layout-manager", "sway", "waykeeper"],
      "fields": ["pid", "comm", "args", "etime", "%cpu", "%mem", "rss"]
    }

The jail-side CLI (``yolo-ps``) sends a JSON request, the daemon runs
``ps`` with *only* the allowlisted comms — never with client-supplied
arg fragments concatenated into argv.  Every request is logged.

Security posture: read-only.  No ``kill``, no environ (leaks secrets),
no ``/proc/<pid>/maps``.  The allowlist is the audit boundary.  A
human operator widening visibility edits ``yolo-jail.jsonc``; the diff
is surfaced at next jail startup.

This daemon is a reference consumer of ``src/host_service.py``; it
stays small by design.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from . import host_service
except ImportError:  # pragma: no cover — running as a script
    from src import host_service  # type: ignore[no-redef]


DEFAULT_FIELDS = ["pid", "comm", "args", "etime", "%cpu", "%mem", "rss"]


log = logging.getLogger("host-processes")


def _load_config(config_path: Path) -> Dict[str, Any]:
    """Read ``host_processes`` from the jsonc config.  Missing file or
    missing section → feature effectively disabled (empty allowlist)."""
    if not config_path.is_file():
        return {"visible": [], "fields": DEFAULT_FIELDS}
    try:
        import pyjson5

        data = pyjson5.loads(config_path.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("%s unreadable: %s — treating as empty", config_path, e)
        return {"visible": [], "fields": DEFAULT_FIELDS}
    hp = data.get("host_processes") or {}
    visible = hp.get("visible") or []
    fields = hp.get("fields") or DEFAULT_FIELDS
    return {
        "visible": [str(x) for x in visible if isinstance(x, str)],
        "fields": [str(x) for x in fields if isinstance(x, str)],
    }


def build_handler(config_path: Path):
    """Return a ``Session`` handler closed over the config.  The
    config is re-read on every request — cheap, and means operator
    edits take effect without a daemon restart.
    """

    def handler(session: host_service.Session) -> None:
        cfg = _load_config(config_path)
        visible = set(cfg["visible"])
        fields = cfg["fields"]
        mode = str(session.request.get("mode") or "list")
        want_pid = session.request.get("pid")

        if not visible:
            session.stderr(
                "host_processes.visible is empty in yolo-jail.jsonc — nothing to show\n"
            )
            session.exit(3)
            return

        if mode == "list":
            # ps -o <fields> -C <comm1> -C <comm2> …
            argv = ["ps", "-o", ",".join(fields)]
            for comm in sorted(visible):
                argv.extend(["-C", comm])
            # Allowlist positions: every string passed after -o or -C needs to
            # be in the allowlist OR be one of the known option keywords.
            known_flags = {"ps", "-o", "-C", ",".join(fields)}
            session.exec_allowlisted(
                lambda _req: argv,
                allowlist=visible | known_flags,
            )
            return

        if mode == "tree":
            # pstree-equivalent. `ps -eHo` then filter to allowlisted comms +
            # their children. Keep it simple: just `ps -eHo` all, let client
            # filter if needed — BUT that leaks host state. So instead: for
            # each allowlisted comm, print it and its children using
            # `pstree -p <pid>`. Simplest safe option: ps with --forest.
            argv = ["ps", "-eo", "pid,ppid,comm,args", "--forest"]
            # No user-provided args here; exec directly.
            try:
                import subprocess

                out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
                # Filter lines to only those whose comm is in the allowlist or
                # whose ppid appears in a collected allowed-pid set. One pass.
                lines = out.stdout.splitlines()
                if not lines:
                    session.exit(0)
                    return
                header = lines[0]
                allowed_pids: set[str] = set()
                kept: List[str] = [header]
                # First pass: direct matches.
                for line in lines[1:]:
                    parts = line.split(None, 3)
                    if len(parts) < 3:
                        continue
                    pid, _ppid, comm = parts[0], parts[1], parts[2].lstrip("\\_ ")
                    if comm in visible:
                        allowed_pids.add(pid)
                        kept.append(line)
                # Second pass: children (ppid in allowed_pids).
                for line in lines[1:]:
                    parts = line.split(None, 3)
                    if len(parts) < 3:
                        continue
                    pid, ppid = parts[0], parts[1]
                    if ppid in allowed_pids and line not in kept:
                        kept.append(line)
                        allowed_pids.add(pid)
                session.stdout("\n".join(kept) + "\n")
                session.exit(0)
                return
            except Exception as e:  # noqa: BLE001
                session.stderr(f"tree mode failed: {e}\n")
                session.exit(1)
                return

        if mode == "pid":
            # Details for one pid — allowlisted only if its comm is visible.
            if not isinstance(want_pid, int):
                session.stderr("pid mode requires integer 'pid' in request\n")
                session.exit(2)
                return
            comm_path = Path(f"/proc/{want_pid}/comm")
            try:
                comm = comm_path.read_text().strip()
            except OSError:
                session.stderr(f"pid {want_pid} not found\n")
                session.exit(1)
                return
            if comm not in visible:
                session.stderr(
                    f"pid {want_pid} has comm={comm!r} which is not allowlisted\n"
                )
                session.exit(2)
                return
            argv = ["ps", "-o", ",".join(fields), "-p", str(want_pid)]
            session.exec_allowlisted(
                lambda _req: argv,
                allowlist={"ps", "-o", ",".join(fields), "-p", str(want_pid), comm},
                argv_positions=set(range(len(argv))),
            )
            return

        session.stderr(f"unknown mode: {mode!r}\n")
        session.exit(2)

    return handler


def self_check() -> int:
    """Cheap health check for ``yolo doctor``."""
    # Config path comes from env — set by `yolo run` when spawning.
    cfg_env = os.environ.get("YOLO_HOST_PROCESSES_CONFIG")
    if not cfg_env:
        print("FAIL: YOLO_HOST_PROCESSES_CONFIG not set (daemon started without it?)")
        return 1
    cfg_path = Path(cfg_env)
    if not cfg_path.is_file():
        print(f"FAIL: config not found at {cfg_path}")
        return 1
    cfg = _load_config(cfg_path)
    if not cfg["visible"]:
        print(f"FAIL: host_processes.visible empty in {cfg_path}")
        return 1
    print(f"OK: {len(cfg['visible'])} comms allowlisted")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--socket", required=False, type=Path)
    parser.add_argument(
        "--config",
        required=False,
        type=Path,
        help="yolo-jail.jsonc path (defaults to $YOLO_HOST_PROCESSES_CONFIG)",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Emit status and exit (used by `yolo doctor`)",
    )
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()

    if args.socket is None:
        print("ERROR: --socket is required", file=sys.stderr)
        return 2
    config = args.config or Path(
        os.environ.get("YOLO_HOST_PROCESSES_CONFIG") or (Path.cwd() / "yolo-jail.jsonc")
    )
    host_service.serve(build_handler(config), args.socket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
