"""Fast-path assertions about tests/test_jail.py's helper constants.

test_jail.py itself is marked ``pytest.mark.slow`` because it spins
up real containers.  These checks are pure module-level reads and
run in milliseconds — they belong in the fast suite so a regression
in the helper constants is caught on every CI run, not only the
slow integration stage.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from tests import test_jail  # noqa: E402


def test_default_jail_timeout_has_cold_start_headroom():
    """The default subprocess timeout for ``run_yolo`` / ``run_yolo_cli``
    / ``run_yolo_direct`` must leave enough headroom for a cold-start
    CI runner (image pull + mise install + loophole spawn + container
    create).  The old 120s default was exceeded by the first
    integration test on every clean run, making CI red on every commit.
    240s is the empirical floor; 300s is what we ship.  This guardrail
    locks the floor so a well-meaning revert can't silently put CI
    back in the red."""
    assert test_jail.DEFAULT_JAIL_TIMEOUT >= 240, (
        f"DEFAULT_JAIL_TIMEOUT={test_jail.DEFAULT_JAIL_TIMEOUT}s is under "
        "the 240s cold-start floor — integration CI will flake."
    )


def _timeout_default(fn) -> int:
    """Read the ``timeout`` default from a helper regardless of whether
    it's positional (``__defaults__``) or keyword-only (``__kwdefaults__``
    — the case when ``timeout`` comes after ``*args``)."""
    kw = getattr(fn, "__kwdefaults__", None) or {}
    if "timeout" in kw:
        return kw["timeout"]
    import inspect

    sig = inspect.signature(fn)
    return sig.parameters["timeout"].default


def test_run_yolo_uses_the_default():
    """Make sure ``run_yolo`` actually picks up the constant — a
    stray ``timeout=120`` override would silently bypass the bump."""
    assert _timeout_default(test_jail.run_yolo) == test_jail.DEFAULT_JAIL_TIMEOUT


def test_run_yolo_cli_uses_the_default():
    assert _timeout_default(test_jail.run_yolo_cli) == test_jail.DEFAULT_JAIL_TIMEOUT


def test_run_yolo_direct_uses_the_default():
    assert _timeout_default(test_jail.run_yolo_direct) == test_jail.DEFAULT_JAIL_TIMEOUT
