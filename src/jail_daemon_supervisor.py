#!/usr/bin/env python3
"""yolo-jail in-jail daemon supervisor.

Reads ``YOLO_JAIL_DAEMONS`` from the environment (a JSON list of
``{"name", "cmd", "restart"}`` entries, populated by the host-side
loopholes loader) and supervises each entry as a subprocess:

- Starts each daemon at jail boot.
- Restarts per ``restart`` policy: ``always``, ``on-failure``, ``no``.
- Forwards SIGTERM / SIGINT to all children, then exits.
- Writes per-daemon stdout / stderr to
  ``~/.local/state/yolo-jail-daemons/<name>.log`` (rotated at 5 MB).

The supervisor itself runs as a child of PID 1 (the jail's entrypoint or
the user's command after ``exec``).  When PID 1 exits, the kernel kills
everything in the container — including the supervisor — so no cleanup
dance is needed.  We handle signals for graceful shutdown during normal
operation (e.g. ``systemctl stop <service>`` on the host).

The protocol between host and jail is deliberately tiny (one env var) so
out-of-tree loopholes don't need to know about the supervisor's
implementation.  See ``docs/loopholes.md`` and ``src/loopholes.py`` for
the manifest side.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


LOG_DIR = Path.home() / ".local" / "state" / "yolo-jail-daemons"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
RESTART_BACKOFF_INITIAL = 1.0
RESTART_BACKOFF_MAX = 30.0


log = logging.getLogger("jail-daemon-sup")


@dataclass
class DaemonSpec:
    name: str
    cmd: List[str]
    restart: str = "on-failure"  # always | on-failure | no


def _parse_env(raw: str) -> List[DaemonSpec]:
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("YOLO_JAIL_DAEMONS not valid JSON: %s", e)
        return []
    out: List[DaemonSpec] = []
    if not isinstance(entries, list):
        log.error("YOLO_JAIL_DAEMONS must be a JSON list, got %s", type(entries))
        return []
    for e in entries:
        if not isinstance(e, dict):
            log.error("skipping non-dict entry: %r", e)
            continue
        name = str(e.get("name") or "")
        cmd = e.get("cmd")
        restart = str(e.get("restart", "on-failure"))
        if not name or not isinstance(cmd, list) or not cmd:
            log.error("skipping invalid entry: %r", e)
            continue
        out.append(DaemonSpec(name=name, cmd=[str(x) for x in cmd], restart=restart))
    return out


def _open_log(name: str):
    """Open (and rotate if needed) the per-daemon log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    p = LOG_DIR / f"{name}.log"
    if p.is_file() and p.stat().st_size > LOG_MAX_BYTES:
        try:
            p.rename(LOG_DIR / f"{name}.log.1")
        except OSError:
            pass
    return open(p, "ab")


class _Child:
    def __init__(self, spec: DaemonSpec):
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.backoff = RESTART_BACKOFF_INITIAL

    def start(self) -> None:
        logfile = _open_log(self.spec.name)
        log.info("starting %s: %s", self.spec.name, " ".join(self.spec.cmd))
        self.proc = subprocess.Popen(
            self.spec.cmd,
            stdout=logfile,
            stderr=logfile,
            close_fds=True,
        )

    def wait_and_maybe_restart(self, stop: threading.Event) -> bool:
        """Wait for the child to exit; return True if it should restart.

        ``stop`` is the global shutdown flag; if it's set we return False
        so the caller exits the loop.
        """
        assert self.proc is not None
        rc = self.proc.wait()
        if stop.is_set():
            return False
        log.info("daemon %s exited rc=%d", self.spec.name, rc)
        if self.spec.restart == "no":
            return False
        if self.spec.restart == "on-failure" and rc == 0:
            return False
        # Exponential backoff capped at RESTART_BACKOFF_MAX so a persistent
        # crash loop doesn't burn CPU.
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, RESTART_BACKOFF_MAX)
        return not stop.is_set()

    def terminate(self, timeout: float = 5.0) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            return
        try:
            self.proc.terminate()
        except OSError:
            return
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except OSError:
                pass


def _supervise_one(child: _Child, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            child.start()
        except OSError as e:
            log.error("failed to spawn %s: %s", child.spec.name, e)
            time.sleep(child.backoff)
            child.backoff = min(child.backoff * 2, RESTART_BACKOFF_MAX)
            continue
        if not child.wait_and_maybe_restart(stop):
            return


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raw = os.environ.get("YOLO_JAIL_DAEMONS", "").strip()
    if not raw:
        log.info("YOLO_JAIL_DAEMONS unset — nothing to supervise")
        return 0
    specs = _parse_env(raw)
    if not specs:
        log.info("no valid daemons to supervise")
        return 0

    stop = threading.Event()
    children = [_Child(s) for s in specs]

    def shutdown(_signo, _frame):
        if stop.is_set():
            return
        log.info("shutdown signal received — terminating daemons")
        stop.set()
        for c in children:
            c.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threads = [
        threading.Thread(
            target=_supervise_one,
            args=(c, stop),
            name=f"sup-{c.spec.name}",
            daemon=True,
        )
        for c in children
    ]
    for t in threads:
        t.start()

    # Idle until SIGTERM / SIGINT.  The daemons run in their own threads;
    # this main thread just waits for stop.
    stop.wait()
    for t in threads:
        t.join(timeout=10.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
