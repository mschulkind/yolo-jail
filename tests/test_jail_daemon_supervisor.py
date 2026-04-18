"""Tests for src.jail_daemon_supervisor — in-jail daemon supervisor."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from src import jail_daemon_supervisor as sup


def test_parse_env_empty_string_returns_empty():
    assert sup._parse_env("") == []
    assert sup._parse_env("not json") == []


def test_parse_env_wrong_type_returns_empty():
    import json

    assert sup._parse_env(json.dumps({"not": "a list"})) == []


def test_parse_env_skips_invalid_entries():
    import json

    raw = json.dumps(
        [
            {"name": "good", "cmd": ["true"], "restart": "on-failure"},
            "not a dict",
            {"name": "", "cmd": ["true"]},  # empty name
            {"name": "no-cmd"},  # missing cmd
            {"name": "bad-cmd", "cmd": []},  # empty cmd
            {"name": "another-good", "cmd": ["echo", "hi"]},
        ]
    )
    specs = sup._parse_env(raw)
    assert [s.name for s in specs] == ["good", "another-good"]


def test_open_log_creates_parent_dir(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "state" / "yolo-jail-daemons"
    monkeypatch.setattr(sup, "LOG_DIR", log_dir)
    f = sup._open_log("mylog")
    try:
        f.write(b"hello\n")
        f.flush()
    finally:
        f.close()
    assert (log_dir / "mylog.log").is_file()
    assert (log_dir / "mylog.log").read_bytes() == b"hello\n"


def test_open_log_rotates_when_oversized(tmp_path: Path, monkeypatch):
    log_dir = tmp_path / "state"
    log_dir.mkdir()
    monkeypatch.setattr(sup, "LOG_DIR", log_dir)
    monkeypatch.setattr(sup, "LOG_MAX_BYTES", 10)
    # Prefill with oversized content.
    (log_dir / "foo.log").write_bytes(b"x" * 100)
    f = sup._open_log("foo")
    try:
        f.write(b"new\n")
    finally:
        f.close()
    # Old file moved to .1; new file contains only new content.
    assert (log_dir / "foo.log.1").is_file()
    assert (log_dir / "foo.log").read_bytes() == b"new\n"


def test_end_to_end_supervisor_starts_and_stops(tmp_path: Path):
    """Launch the supervisor as a subprocess, verify it starts a daemon
    that writes to its log, then SIGTERMs it and confirms clean exit."""
    import json
    import signal

    marker = tmp_path / "marker"
    # Daemon: one shot, writes marker, exits 0.
    daemon_cmd = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ok'); "
        f"import time; time.sleep(30)",
    ]
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "YOLO_JAIL_DAEMONS": json.dumps(
            [{"name": "test", "cmd": daemon_cmd, "restart": "no"}]
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.jail_daemon_supervisor"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait up to 5s for the daemon to write its marker.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.is_file(), "daemon did not write marker"
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0
