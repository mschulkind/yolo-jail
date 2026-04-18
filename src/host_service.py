"""yolo-jail host_service — helper for writing unix-socket loophole daemons.

A unix-socket loophole (spawned lifecycle) is a host-side daemon that
accepts requests from the jail over a Unix socket and replies with a
framed byte stream.  This module owns the boring bits so each daemon
shrinks to a handler function plus its allowlist.

Usage — the whole API surface is ``serve`` + ``Session``:

.. code-block:: python

    from src.host_service import serve, Session

    ALLOWED_COMMS = {"layout-manager", "sway"}

    def handle(session: Session) -> None:
        # session.request is the parsed JSON the client sent.
        comm = session.request.get("comm")
        if comm not in ALLOWED_COMMS:
            session.stderr(f"comm {comm!r} not allowlisted\\n")
            session.exit(2)
            return
        # Build argv from our own allowlist — never from client input.
        session.exec_allowlisted(
            lambda _req: ["ps", "-o", "pid,etime,comm,args", "-C", comm],
            allowlist=ALLOWED_COMMS,
        )

    if __name__ == "__main__":
        serve(handle, socket_path=Path(sys.argv[1]))

Design notes
------------

* **Frame protocol v1** — see ``docs/loophole-protocol.md`` for the wire
  spec.  Briefly: each frame is ``<1-byte stream_id><4-byte big-endian
  length><length bytes>``; stream_id ∈ {0=stdout, 1=stderr, 2=exit}.
  The exit frame's payload is a single big-endian int32 return code.
* **Access logging** — every request logs one structured line with the
  jail id (from ``YOLO_JAIL_ID`` on the client side, passed in the first
  request frame), the request summary, elapsed time, and bytes tx/rx.
  No opt-in needed; daemons get auditability for free.
* **Command-injection guard** — ``Session.exec_allowlisted`` takes an
  ``argv_builder`` that receives the client request and returns an argv
  list, AND a sanity ``allowlist`` that the library validates every
  element of argv[0] and strategic arg positions against.  Daemons that
  skip this helper and shell out manually are on their own; the helper
  makes the safe path the short path.
* **One thread per connection** — cheap, stdlib-only, no asyncio.  A
  loophole daemon should be serving a handful of jails at most.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set


PROTOCOL_VERSION = 1

STREAM_STDOUT = 0
STREAM_STDERR = 1
STREAM_EXIT = 2

_FRAME_HEADER = struct.Struct(">BI")  # stream_id, length


log = logging.getLogger("host_service")


# ---------------------------------------------------------------------------
# Session — one per connected client
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """Single client connection.  Handlers receive one of these and drive
    it with ``stdout/stderr/json/exit/exec_allowlisted``."""

    request: Dict[str, Any]
    jail_id: str
    _conn: socket.socket
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _bytes_out: int = 0
    _exited: bool = False

    def _send_frame(self, stream_id: int, payload: bytes) -> None:
        if self._exited:
            return
        with self._lock:
            self._conn.sendall(_FRAME_HEADER.pack(stream_id, len(payload)))
            if payload:
                self._conn.sendall(payload)
            self._bytes_out += len(payload) + _FRAME_HEADER.size

    def stdout(self, data: "str | bytes") -> None:
        """Frame-write to the client's stdout stream."""
        if isinstance(data, str):
            data = data.encode()
        self._send_frame(STREAM_STDOUT, data)

    def stderr(self, data: "str | bytes") -> None:
        """Frame-write to the client's stderr stream."""
        if isinstance(data, str):
            data = data.encode()
        self._send_frame(STREAM_STDERR, data)

    def json(self, obj: Any) -> None:
        """Emit ``obj`` as one newline-terminated JSON line on stdout.

        This is the default output format jail-side CLIs should reach
        for — agents parse JSON; humans can use ``--table`` / ``--tree``
        flags on the client to pretty-print.
        """
        self.stdout(json.dumps(obj) + "\n")

    def exit(self, code: int) -> None:
        """End the session with an exit code.  Idempotent."""
        if self._exited:
            return
        payload = struct.pack(">i", int(code))
        self._send_frame(STREAM_EXIT, payload)
        self._exited = True

    def exec_allowlisted(
        self,
        argv_builder: Callable[[Dict[str, Any]], List[str]],
        *,
        allowlist: Iterable[str],
        argv_positions: Optional[Iterable[int]] = None,
        timeout: Optional[float] = 30.0,
    ) -> int:
        """Run an external command whose argv is built from an allowlist.

        ``argv_builder`` receives the request and returns the argv.  The
        library enforces that every string in ``argv_builder``'s output
        whose position appears in ``argv_positions`` (default: last-position
        args 1..n where position 0 is the executable) belongs to
        ``allowlist``.  This prevents client-provided selectors from
        escaping into shell arguments.

        Streams the child's stdout and stderr back through ``Session``
        frames so the client sees progress live.  Returns the child's
        exit code; also calls ``self.exit(code)`` so the handler can
        ``return`` immediately after.
        """
        argv = argv_builder(self.request)
        allowset: Set[str] = set(allowlist)
        positions = (
            set(argv_positions)
            if argv_positions is not None
            else set(range(1, len(argv)))
        )
        for i, arg in enumerate(argv):
            if i in positions and arg not in allowset:
                self.stderr(f"exec_allowlisted: argv[{i}]={arg!r} not in allowlist\n")
                self.exit(2)
                return 2

        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        stop = threading.Event()

        def _pump(src, stream_id):
            assert src is not None
            try:
                while not stop.is_set():
                    chunk = src.read(4096)
                    if not chunk:
                        break
                    self._send_frame(stream_id, chunk)
            finally:
                src.close()

        t_out = threading.Thread(target=_pump, args=(proc.stdout, STREAM_STDOUT))
        t_err = threading.Thread(target=_pump, args=(proc.stderr, STREAM_STDERR))
        t_out.start()
        t_err.start()

        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = 124
            self.stderr("exec_allowlisted: timed out\n")
        stop.set()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        self.exit(rc)
        return rc


# ---------------------------------------------------------------------------
# serve — socket setup, accept loop, per-connection threading
# ---------------------------------------------------------------------------


Handler = Callable[[Session], None]


def _read_exact(conn: socket.socket, n: int) -> "bytes | None":
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_request(conn: socket.socket) -> "Optional[Dict[str, Any]]":
    """Read a single length-prefixed JSON request.  Returns None on
    clean EOF before a complete request arrived.
    """
    header = _read_exact(conn, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    body = _read_exact(conn, length)
    if body is None:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _handle_one(handler: Handler, conn: socket.socket, addr_str: str) -> None:
    """Receive one request, invoke the handler, log the summary."""
    start = time.monotonic()
    request: Dict[str, Any] = {}
    jail_id = "unknown"
    rc_for_log: "Optional[int]" = None
    session: Optional[Session] = None
    try:
        parsed = _read_request(conn)
        if parsed is None:
            log.info("conn=%s closed without a request", addr_str)
            return
        request = parsed
        jail_id = str(request.get("jail_id") or "unknown")
        session = Session(request=request, jail_id=jail_id, _conn=conn)
        try:
            handler(session)
            session.exit(0)  # default exit if handler didn't
            rc_for_log = 0
        except Exception as e:
            log.exception("handler raised: %s", e)
            try:
                session.stderr(f"handler error: {e}\n")
                session.exit(1)
            except OSError:
                pass
            rc_for_log = 1
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        # Elide the full request — could be big / sensitive. Log the keys.
        req_keys = sorted(request.keys()) if isinstance(request, dict) else []
        bytes_out = session._bytes_out if session else 0
        log.info(
            "jail=%s keys=%s rc=%s elapsed_ms=%d bytes_out=%d",
            jail_id,
            ",".join(req_keys) or "-",
            rc_for_log,
            elapsed_ms,
            bytes_out,
        )
        try:
            conn.close()
        except OSError:
            pass


def serve(
    handler: Handler,
    socket_path: Path,
    *,
    log_path: Optional[Path] = None,
    log_level: int = logging.INFO,
    backlog: int = 16,
) -> None:
    """Serve on a Unix socket until SIGTERM / SIGINT.

    ``socket_path`` is created with mode 0600 and removed on exit.  If
    ``log_path`` is given, a rotating-agnostic FileHandler is attached
    at ``log_level``; otherwise logs go to stderr.  Thread-per-connection
    keeps the implementation boring — callers that need more can wrap.
    """
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_path))
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        log.addHandler(fh)
    else:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    log.setLevel(log_level)

    if socket_path.exists():
        try:
            socket_path.unlink()
        except OSError:
            pass
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        old_umask = os.umask(0o077)
        try:
            sock.bind(str(socket_path))
        finally:
            os.umask(old_umask)
        os.chmod(socket_path, 0o600)
        sock.listen(backlog)
        log.info("listening on %s (protocol v%d)", socket_path, PROTOCOL_VERSION)

        stop = threading.Event()

        def _graceful(_signo, _frame):
            log.info("signal received, shutting down")
            stop.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # Signal handlers can only be installed from the main thread.
        # Tests (and any embedded use) may invoke serve() from a worker
        # thread; there we just rely on socket close / GC for shutdown.
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _graceful)
            signal.signal(signal.SIGINT, _graceful)

        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                break
            threading.Thread(
                target=_handle_one,
                args=(handler, conn, f"fd{conn.fileno()}"),
                daemon=True,
            ).start()
    finally:
        try:
            sock.close()
        except OSError:
            pass
        try:
            socket_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Convenience CLI for testing handlers locally
# ---------------------------------------------------------------------------


def _main_for_smoke_test() -> None:
    """python -m src.host_service /tmp/smoke.sock — a no-op handler for
    verifying socket setup / frame protocol without writing a real
    daemon.  Replies with ``{"ok": true}`` and exits 0."""
    if len(sys.argv) < 2:
        print("usage: python -m src.host_service <socket-path>", file=sys.stderr)
        sys.exit(2)
    sock_path = Path(sys.argv[1])

    def smoke(session: Session) -> None:
        session.json({"ok": True, "echo": session.request})

    serve(smoke, sock_path)


if __name__ == "__main__":
    _main_for_smoke_test()
