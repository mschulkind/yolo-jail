"""Regression tests for the doctor's handling of inactive loopholes.

Two bugs that were surfacing on CI (where no one has run ``just
deploy`` and the broker's state dir is empty):

1. ``_check_loopholes`` runs ``doctor_cmd`` for loopholes whose
   ``requires`` predicate isn't met.  Should skip — those loopholes
   are present-but-inactive, their doctor check is meaningless.

2. ``oauth_broker.self_check`` treats missing CA/leaf state as a FAIL
   (rc=1).  Missing state is a normal pre-deploy state; self_check
   should return 0 with a warning message so ``yolo check`` doesn't
   trip over it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import loopholes, oauth_broker


def _write_manifest(path: Path, data: dict) -> None:
    (path / "manifest.jsonc").write_text(json.dumps(data, indent=2))


@pytest.fixture
def broker_state(tmp_path: Path, monkeypatch):
    """Point the broker at an empty state dir — mimics a fresh host."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(oauth_broker, "BROKER_DIR", state)
    monkeypatch.setattr(oauth_broker, "CA_CRT", state / "ca.crt")
    monkeypatch.setattr(oauth_broker, "CA_KEY", state / "ca.key")
    monkeypatch.setattr(oauth_broker, "SERVER_CRT", state / "server.crt")
    monkeypatch.setattr(oauth_broker, "SERVER_KEY", state / "server.key")
    return state


# ---------------------------------------------------------------------------
# Bug 1: _check_loopholes should skip doctor_cmd for inactive loopholes
# ---------------------------------------------------------------------------


def test_check_loopholes_skips_inactive(tmp_path: Path, monkeypatch, capsys):
    """Ready-bundled loophole whose ``requires`` predicate isn't met must
    not have its doctor_cmd executed — that binary is legitimately
    absent when the loophole is inactive."""
    mods_root = tmp_path / "loopholes"
    mods_root.mkdir()
    mod = mods_root / "needs-xyz"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "needs-xyz",
            "description": "x",
            "requires": {"command_on_path": "xyz-never-exists-abc"},
            # A doctor_cmd that would FAIL if actually invoked — proving
            # the check short-circuits.
            "doctor_cmd": [
                "/no/such/binary/that/exists/anywhere",
                "--self-check",
            ],
        },
    )

    monkeypatch.setattr(loopholes, "user_loopholes_dir", lambda: mods_root)
    monkeypatch.setattr(loopholes, "bundled_loopholes_dir", lambda: tmp_path / "nobdl")

    from src import cli

    calls = []

    def ok(msg, *a, **kw):
        calls.append(("ok", msg))

    def warn(msg, note="", *a, **kw):
        calls.append(("warn", msg, note))

    def fail(msg, note="", *a, **kw):
        calls.append(("fail", msg, note))

    # Simulate running on the host (not inside a jail).
    monkeypatch.delenv("YOLO_VERSION", raising=False)
    cli._check_loopholes(ok, warn, fail)

    # No fail should have been emitted; the inactive loophole should be
    # reported but SKIPPED (no doctor_cmd invocation).
    fails = [c for c in calls if c[0] == "fail"]
    assert fails == [], f"expected no fails, got {fails}"
    # And we should see a message acknowledging the inactive state.
    inactive_msgs = [c for c in calls if "inactive" in c[1].lower()]
    assert inactive_msgs, f"expected an 'inactive' report, got {calls}"


# ---------------------------------------------------------------------------
# Bug 2: broker self_check returns 0 when only state is missing
# ---------------------------------------------------------------------------


def test_broker_self_check_ok_when_state_missing(broker_state, capsys, monkeypatch):
    """Fresh host: no ``just deploy`` run, so no CA/leaf in state dir.
    That's not a failure — it's the pre-deploy normal, and the user is
    about to run ``just deploy`` to prime state.  self_check should
    surface the missing state as a warning, not a fatal rc=1."""
    # Pretend openssl is installed; we're testing the state-missing
    # path, not the openssl-missing-and-can't-recover path.
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _x: "/usr/bin/openssl")
    import tempfile

    creds = Path(tempfile.mkdtemp(prefix="yjt-")) / "creds.json"
    creds.write_text('{"claudeAiOauth": {"accessToken": "x", "expiresAt": 0}}')
    monkeypatch.setattr(oauth_broker, "DEFAULT_CREDS_PATH", creds)

    rc = oauth_broker.self_check()
    out = capsys.readouterr().out
    assert rc == 0, (
        f"self_check should tolerate missing state, got rc={rc} output={out!r}"
    )
    # And it should still SAY something about the missing state — the
    # operator needs a hint that ``--init-ca`` is their next step.
    assert "ca.crt" in out.lower() or "init-ca" in out.lower()


def test_broker_self_check_fails_when_openssl_and_state_both_missing(
    broker_state, capsys, monkeypatch
):
    """If state is missing AND openssl is missing, the user can't
    recover via --init-ca.  That's a real failure that should hard-fail
    doctor, not just warn."""
    monkeypatch.setattr(oauth_broker.shutil, "which", lambda _x: None)
    import tempfile

    creds = Path(tempfile.mkdtemp(prefix="yjt-")) / "creds.json"
    creds.write_text('{"claudeAiOauth": {}}')
    monkeypatch.setattr(oauth_broker, "DEFAULT_CREDS_PATH", creds)

    rc = oauth_broker.self_check()
    assert rc == 1
