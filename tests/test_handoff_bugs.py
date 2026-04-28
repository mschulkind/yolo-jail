"""Regression tests for the yolo-jail bugs the other agent found
in scratch/yolo-jail-handoff.md.  Each test is failing on the
pre-fix code and locks in the fix going forward.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cli import (  # noqa: E402
    KNOWN_TOP_LEVEL_CONFIG_KEYS,
    _validate_config,
)


# ---------------------------------------------------------------------------
# Bug 3 — ``host_processes`` top-level config key rejected by schema
# ---------------------------------------------------------------------------


def test_host_processes_top_level_key_accepted():
    """The ``host-processes`` loophole reads ``host_processes.visible``
    from the top-level of yolo-jail.jsonc (see src/host_processes.py).
    The schema validator must accept that key — otherwise users who
    follow yolo-ps's own advice get a validation failure."""
    assert "host_processes" in KNOWN_TOP_LEVEL_CONFIG_KEYS

    config = {
        "host_processes": {
            "visible": ["sway", "layout-manager"],
            "fields": ["pid", "comm", "args"],
        },
        "runtime": "podman",
    }
    errors, _warnings = _validate_config(config)
    # No errors whatsoever about host_processes.
    hp_errors = [e for e in errors if "host_processes" in e]
    assert hp_errors == [], f"unexpected host_processes errors: {hp_errors}"


def test_host_processes_visible_type_validated():
    """Not just any shape — visible must be a list, fields must be a list."""
    config = {
        "host_processes": {"visible": "sway"},  # wrong — should be list
    }
    errors, _warnings = _validate_config(config)
    hp_errors = [e for e in errors if "host_processes" in e]
    assert hp_errors, "expected a schema error for non-list 'visible'"


def test_host_processes_unknown_subkey_rejected():
    """Typo'd subkey under host_processes should produce a schema error."""
    config = {
        "host_processes": {"visible": [], "wtf_is_this": 42},
    }
    errors, _warnings = _validate_config(config)
    assert any("wtf_is_this" in e or "host_processes" in e for e in errors)


# ---------------------------------------------------------------------------
# Bug 4 — In-jail yolo check shouldn't exec-check host paths
# ---------------------------------------------------------------------------


def test_check_skips_loophole_exec_check_inside_jail(tmp_path, monkeypatch, capsys):
    """Inside a jail, host paths in ``loopholes:`` config legitimately
    don't exist in the jail filesystem — the exec-presence check is a
    false negative.  It must be skipped when YOLO_VERSION is set,
    matching the pattern already used for the doctor section."""
    from src import cli

    monkeypatch.setenv("YOLO_VERSION", "test-0.0.0")

    # Simulate the check-section code that runs the exec check.
    calls = []

    def fail(msg, note=""):
        calls.append(("fail", msg))

    def ok(msg):
        calls.append(("ok", msg))

    def warn(msg, note=""):
        calls.append(("warn", msg))

    # Host paths referenced in ``loopholes:`` can't exist inside the
    # jail — the fix is a predicate that short-circuits the exec
    # check.  Assert the predicate exists and fires in-jail.
    skipped = cli._loophole_exec_checks_skipped_in_jail()
    assert skipped, "exec checks must auto-skip inside the jail"


# ---------------------------------------------------------------------------
# Bug 5 — cosmetic: "host_services" still appears in user-facing strings
# ---------------------------------------------------------------------------


def test_no_host_services_in_user_facing_text():
    """Post-rename the string ``host_services`` shouldn't appear in
    any message the operator sees.  The config key is ``loopholes``
    now.  Internal variable names aren't the concern; visible text is."""
    cli_path = Path(__file__).parent.parent / "src" / "cli.py"
    src = cli_path.read_text()
    # Find user-facing text (inside f"..." / "..." used with fail/ok/warn/echo).
    # Simple check: no ``host_services.`` substring in any string literal
    # that also contains a brace-format placeholder.
    import re

    bad = re.findall(r'"host_services\.\{[^"]*"', src)
    assert bad == [], f"user-facing host_services strings still present: {bad}"
