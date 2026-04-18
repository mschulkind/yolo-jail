"""Tests for src.loopholes — the host-side loophole registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import loopholes


def _write_manifest(path: Path, data: dict) -> None:
    (path / "manifest.jsonc").write_text(json.dumps(data, indent=2))


@pytest.fixture
def mods_dir(tmp_path: Path) -> Path:
    root = tmp_path / "loopholes"
    root.mkdir()
    return root


def test_discover_empty_dir_returns_empty(mods_dir: Path):
    assert loopholes.discover_loopholes(mods_dir, include_bundled=False) == []


def test_discover_nonexistent_returns_empty(tmp_path: Path):
    assert (
        loopholes.discover_loopholes(tmp_path / "does-not-exist", include_bundled=False)
        == []
    )


def test_loads_minimal_manifest(mods_dir: Path):
    mod = mods_dir / "my-mod"
    mod.mkdir()
    _write_manifest(mod, {"name": "my-mod", "description": "test"})

    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    assert len(loaded) == 1
    assert loaded[0].name == "my-mod"
    assert loaded[0].enabled is True
    assert loaded[0].transport == "tls-intercept"
    assert loaded[0].lifecycle == "external"
    assert loaded[0].intercepts == []
    assert loaded[0].ca_cert is None


def test_name_must_match_directory(mods_dir: Path):
    mod = mods_dir / "dir-name"
    mod.mkdir()
    _write_manifest(mod, {"name": "different-name", "description": "x"})
    assert loopholes.discover_loopholes(mods_dir, include_bundled=False) == []
    entries = loopholes.validate_loopholes(mods_dir, include_bundled=False)
    assert len(entries) == 1
    _, loophole, err = entries[0]
    assert loophole is None
    assert err is not None and "disagrees with directory" in err


def test_disabled_skipped_by_default(mods_dir: Path):
    mod = mods_dir / "off"
    mod.mkdir()
    _write_manifest(mod, {"name": "off", "description": "x", "enabled": False})

    assert loopholes.discover_loopholes(mods_dir, include_bundled=False) == []
    included = loopholes.discover_loopholes(
        mods_dir, include_bundled=False, include_disabled=True
    )
    assert len(included) == 1 and included[0].name == "off"


def test_invalid_transport_rejected(mods_dir: Path):
    mod = mods_dir / "bad-transport"
    mod.mkdir()
    _write_manifest(
        mod,
        {"name": "bad-transport", "description": "x", "transport": "carrier-pigeon"},
    )
    entries = loopholes.validate_loopholes(mods_dir, include_bundled=False)
    _, loophole, err = entries[0]
    assert loophole is None
    assert err is not None and "transport=" in err


def test_invalid_lifecycle_rejected(mods_dir: Path):
    mod = mods_dir / "bad-lifecycle"
    mod.mkdir()
    _write_manifest(
        mod,
        {"name": "bad-lifecycle", "description": "x", "lifecycle": "orbiting"},
    )
    entries = loopholes.validate_loopholes(mods_dir, include_bundled=False)
    _, loophole, err = entries[0]
    assert loophole is None
    assert err is not None and "lifecycle=" in err


def test_docker_args_intercept_and_ca(mods_dir: Path):
    mod = mods_dir / "broker"
    mod.mkdir()
    ca = mod / "ca.crt"
    ca.write_text("-----FAKE CA-----\n")
    _write_manifest(
        mod,
        {
            "name": "broker",
            "description": "x",
            "intercepts": [{"host": "example.test"}, {"host": "api.example.test"}],
            "broker_ip": "10.0.0.1",
            "ca_cert": "ca.crt",
            "jail_env": {"FOO": "bar"},
        },
    )
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    args = loopholes.docker_args_for(loaded)
    assert args.count("--add-host") == 2
    assert "example.test:10.0.0.1" in args
    assert "api.example.test:10.0.0.1" in args
    assert any(f"{ca}:/etc/yolo-jail/loopholes/broker/ca.crt:ro" in a for a in args)
    assert "FOO=bar" in args
    assert any(a.startswith("NODE_EXTRA_CA_CERTS=") for a in args)


def test_docker_args_no_ca_no_env(mods_dir: Path):
    mod = mods_dir / "plain"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "plain",
            "description": "x",
            "intercepts": [{"host": "plain.test"}],
        },
    )
    args = loopholes.docker_args_for(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    assert args == ["--add-host", "plain.test:host-gateway"]


def test_docker_args_skip_config_backed_loopholes(tmp_path: Path):
    # Config-backed (synthesized from yolo-jail.jsonc) loopholes have no
    # file-backed mounts or intercepts — their wiring lives in
    # cli.start_loopholes.  docker_args_for should ignore them entirely.
    loaded = loopholes.discover_loopholes(
        tmp_path / "empty",
        include_bundled=False,
        loopholes_config={"journal": {"description": "x"}},
    )
    assert loopholes.docker_args_for(loaded) == []


def test_multiple_loopholes_merge_ca_paths(mods_dir: Path):
    for name in ("a", "b"):
        mod = mods_dir / name
        mod.mkdir()
        (mod / "ca.crt").write_text(f"ca-for-{name}")
        _write_manifest(
            mod,
            {"name": name, "description": "x", "ca_cert": "ca.crt"},
        )
    args = loopholes.docker_args_for(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    node_ca = next(a for a in args if a.startswith("NODE_EXTRA_CA_CERTS="))
    assert "/etc/yolo-jail/loopholes/a/ca.crt" in node_ca
    assert "/etc/yolo-jail/loopholes/b/ca.crt" in node_ca


def test_set_enabled_roundtrip(mods_dir: Path):
    mod = mods_dir / "togg"
    mod.mkdir()
    _write_manifest(
        mod,
        {"name": "togg", "description": "x", "enabled": True},
    )

    loopholes.set_enabled(mod, False)
    assert loopholes.discover_loopholes(mods_dir, include_bundled=False) == []
    assert (
        len(
            loopholes.discover_loopholes(
                mods_dir, include_bundled=False, include_disabled=True
            )
        )
        == 1
    )

    loopholes.set_enabled(mod, True)
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    assert len(loaded) == 1 and loaded[0].enabled is True


def test_invalid_manifest_does_not_break_others(mods_dir: Path):
    good = mods_dir / "good"
    good.mkdir()
    _write_manifest(good, {"name": "good", "description": "x"})
    bad = mods_dir / "bad"
    bad.mkdir()
    (bad / "manifest.jsonc").write_text("{not: json")
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    assert [m.name for m in loaded] == ["good"]


def test_hidden_dirs_skipped(mods_dir: Path):
    hidden = mods_dir / ".git"
    hidden.mkdir()
    _write_manifest(hidden, {"name": ".git", "description": "x"})
    assert loopholes.discover_loopholes(mods_dir, include_bundled=False) == []


def test_loopholes_config_synthesized_as_loopholes(mods_dir: Path):
    loopholes_config = {
        "journal": {"description": "journalctl bridge"},
        "cgroup-delegate": {"description": "cgroup v2 delegate"},
    }
    loaded = loopholes.discover_loopholes(
        mods_dir, include_bundled=False, loopholes_config=loopholes_config
    )
    names = [m.name for m in loaded]
    assert "journal" in names
    assert "cgroup-delegate" in names
    for m in loaded:
        assert m.transport == "unix-socket"
        assert m.lifecycle == "spawned"
        assert m.from_config


def test_loopholes_config_synthesized_do_not_emit_docker_args(mods_dir: Path):
    loopholes_config = {"journal": {"description": "x"}}
    loaded = loopholes.discover_loopholes(
        mods_dir, include_bundled=False, loopholes_config=loopholes_config
    )
    # synthesized entries are for display only; their docker wiring is
    # the existing start_loopholes pipeline.
    assert loopholes.docker_args_for(loaded) == []


def test_run_doctor_checks_no_cmd(mods_dir: Path):
    mod = mods_dir / "nocmd"
    mod.mkdir()
    _write_manifest(mod, {"name": "nocmd", "description": "x"})
    results = loopholes.run_doctor_checks(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    assert len(results) == 1
    assert results[0].returncode is None


def test_run_doctor_checks_success(mods_dir: Path):
    mod = mods_dir / "truecmd"
    mod.mkdir()
    _write_manifest(
        mod,
        {"name": "truecmd", "description": "x", "doctor_cmd": ["true"]},
    )
    results = loopholes.run_doctor_checks(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    assert results[0].returncode == 0


def test_run_doctor_checks_failure(mods_dir: Path):
    mod = mods_dir / "falsecmd"
    mod.mkdir()
    _write_manifest(
        mod,
        {"name": "falsecmd", "description": "x", "doctor_cmd": ["false"]},
    )
    results = loopholes.run_doctor_checks(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    assert results[0].returncode == 1


def test_manifest_parses_jail_daemon(mods_dir: Path):
    mod = mods_dir / "with-jd"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "with-jd",
            "description": "x",
            "jail_daemon": {
                "cmd": ["python3", "-m", "src.somemod"],
                "restart": "always",
            },
        },
    )
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    assert len(loaded) == 1
    jd = loaded[0].jail_daemon
    assert jd is not None
    assert jd.cmd == ["python3", "-m", "src.somemod"]
    assert jd.restart == "always"


def test_manifest_rejects_invalid_restart(mods_dir: Path):
    mod = mods_dir / "bad-restart"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "bad-restart",
            "description": "x",
            "jail_daemon": {"cmd": ["true"], "restart": "whenever"},
        },
    )
    _, loophole, err = loopholes.validate_loopholes(mods_dir, include_bundled=False)[0]
    assert loophole is None
    assert err is not None and "restart" in err


def test_manifest_parses_host_daemon(mods_dir: Path):
    mod = mods_dir / "with-hd"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "with-hd",
            "description": "x",
            "host_daemon": {
                "cmd": ["daemon-bin", "--socket", "{socket}"],
                "env": {"FOO": "bar"},
            },
        },
    )
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    hd = loaded[0].host_daemon
    assert hd is not None
    assert hd.cmd == ["daemon-bin", "--socket", "{socket}"]
    assert hd.env == {"FOO": "bar"}


def test_docker_args_mounts_dir_for_jail_daemon(mods_dir: Path):
    mod = mods_dir / "jd-mod"
    mod.mkdir()
    (mod / "ca.crt").write_text("ca")
    (mod / "jail.py").write_text("# jail daemon impl")
    _write_manifest(
        mod,
        {
            "name": "jd-mod",
            "description": "x",
            "intercepts": [{"host": "example.test"}],
            "broker_ip": "127.0.0.1",
            "ca_cert": "ca.crt",
            "jail_daemon": {
                "cmd": ["python3", "/etc/yolo-jail/loopholes/jd-mod/jail.py"],
                "restart": "on-failure",
            },
        },
    )
    args = loopholes.docker_args_for(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    # Dir mount covers the CA too — only one -v line per loophole.
    mount_lines = [a for a in args if "loopholes/jd-mod" in a]
    assert any(a.endswith(":ro") for a in mount_lines)
    # YOLO_JAIL_DAEMONS env var carries the daemon spec.
    jd_env = next(a for a in args if a.startswith("YOLO_JAIL_DAEMONS="))
    import json as _json

    payload = _json.loads(jd_env[len("YOLO_JAIL_DAEMONS=") :])
    assert payload == [
        {
            "name": "jd-mod",
            "cmd": ["python3", "/etc/yolo-jail/loopholes/jd-mod/jail.py"],
            "restart": "on-failure",
        }
    ]
    # CA is still trusted, just via the dir-mount path.
    node_ca = next(a for a in args if a.startswith("NODE_EXTRA_CA_CERTS="))
    assert "/etc/yolo-jail/loopholes/jd-mod/ca.crt" in node_ca


def test_manifest_host_daemon_specs_shaped_like_loopholes_config(mods_dir: Path):
    mod = mods_dir / "with-hd"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "with-hd",
            "description": "the broker host daemon",
            "host_daemon": {"cmd": ["daemon", "--socket", "{socket}"]},
        },
    )
    specs = loopholes.manifest_host_daemon_specs(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    assert specs == {
        "with-hd": {
            "command": ["daemon", "--socket", "{socket}"],
            "description": "the broker host daemon",
        }
    }


def test_requires_command_on_path_inactive_when_missing(mods_dir: Path, monkeypatch):
    mod = mods_dir / "needs-xyz"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "needs-xyz",
            "description": "x",
            "requires": {"command_on_path": "xyz-never-exists-abc"},
        },
    )
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    assert len(loaded) == 1
    assert loaded[0].enabled is True
    assert loaded[0].requirements_met is False
    assert loaded[0].active is False
    assert loaded[0].inactive_reason is not None
    assert "xyz-never-exists-abc" in loaded[0].inactive_reason


def test_requires_command_on_path_active_when_present(mods_dir: Path):
    mod = mods_dir / "needs-sh"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "needs-sh",
            "description": "x",
            "requires": {"command_on_path": "sh"},  # always on path
        },
    )
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    assert loaded[0].requirements_met is True
    assert loaded[0].active is True
    assert loaded[0].inactive_reason is None


def test_inactive_loopholes_skipped_in_docker_args(mods_dir: Path):
    mod = mods_dir / "inactive-mod"
    mod.mkdir()
    (mod / "ca.crt").write_text("ca")
    _write_manifest(
        mod,
        {
            "name": "inactive-mod",
            "description": "x",
            "intercepts": [{"host": "example.test"}],
            "ca_cert": "ca.crt",
            "requires": {"command_on_path": "xyz-definitely-missing"},
        },
    )
    loaded = loopholes.discover_loopholes(mods_dir, include_bundled=False)
    # Present but inactive — docker_args_for emits nothing.
    assert loopholes.docker_args_for(loaded) == []


def test_workspace_override_merges_enabled(mods_dir: Path):
    mod = mods_dir / "bundled-like"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "bundled-like",
            "description": "x",
            "enabled": False,  # off by default
        },
    )
    # Workspace flips it on.
    loaded = loopholes.discover_loopholes(
        mods_dir,
        include_bundled=False,
        include_disabled=True,
        loopholes_config={"bundled-like": {"enabled": True}},
    )
    # The existing entry was mutated, NOT duplicated as a config entry.
    assert len(loaded) == 1
    assert loaded[0].name == "bundled-like"
    assert loaded[0].enabled is True
    assert loaded[0].source == loopholes.SOURCE_USER


def test_workspace_override_merges_host_daemon_env(mods_dir: Path):
    mod = mods_dir / "swaymsg-like"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "swaymsg-like",
            "description": "x",
            "host_daemon": {
                "cmd": ["some-daemon", "--socket", "{socket}"],
                "env": {"DEFAULT_KEY": "default"},
            },
        },
    )
    loaded = loopholes.discover_loopholes(
        mods_dir,
        include_bundled=False,
        loopholes_config={
            "swaymsg-like": {"env": {"SWAYSOCK": "/run/user/1000/sway.sock"}},
        },
    )
    assert len(loaded) == 1
    assert loaded[0].host_daemon is not None
    # Existing env preserved + workspace additions merged in.
    assert loaded[0].host_daemon.env == {
        "DEFAULT_KEY": "default",
        "SWAYSOCK": "/run/user/1000/sway.sock",
    }


def test_workspace_inline_when_no_matching_manifest(mods_dir: Path):
    # No matching file-backed loophole → treated as inline config-backed.
    loaded = loopholes.discover_loopholes(
        mods_dir,
        include_bundled=False,
        loopholes_config={"pure-workspace": {"description": "new inline"}},
    )
    assert len(loaded) == 1
    assert loaded[0].name == "pure-workspace"
    assert loaded[0].from_config is True
    assert loaded[0].source == loopholes.SOURCE_CONFIG


def test_bundled_loopholes_discovered_by_default():
    # claude-oauth-broker ships with the wheel and should be discoverable
    # without include_bundled=True (which is the default).
    loaded = loopholes.discover_loopholes(include_disabled=True)
    names = [m.name for m in loaded]
    assert "claude-oauth-broker" in names
    broker = next(m for m in loaded if m.name == "claude-oauth-broker")
    assert broker.source == loopholes.SOURCE_BUNDLED


def test_user_overrides_bundled_by_name(tmp_path: Path):
    # User-installed loophole with same name as bundled takes precedence.
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    overlay = user_dir / "claude-oauth-broker"
    overlay.mkdir()
    _write_manifest(
        overlay,
        {
            "name": "claude-oauth-broker",
            "description": "local override",
            "enabled": False,
        },
    )
    loaded = loopholes.discover_loopholes(root=user_dir, include_disabled=True)
    broker = [m for m in loaded if m.name == "claude-oauth-broker"]
    assert len(broker) == 1  # not duplicated
    assert broker[0].source == loopholes.SOURCE_USER
    assert broker[0].description == "local override"


def test_no_file_overlay_on_dir_mount(mods_dir: Path, tmp_path: Path, monkeypatch):
    """Regression: podman rejects container specs that mount a file INTO
    a path already covered by a dir mount ("conmon bytes '': readObjectStart").
    When the CA lives inside a mounted dir (bundled loophole dir OR the
    state dir), docker_args_for must reference the existing mount — not
    stack a second file mount on top."""
    mod = mods_dir / "has-jail"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "has-jail",
            "description": "x",
            "intercepts": [{"host": "example.test"}],
            "broker_ip": "127.0.0.1",
            "ca_cert": "{state}/ca.crt",
            "jail_daemon": {"cmd": ["true"], "restart": "no"},
        },
    )
    # Fake a state dir with a ca.crt so the state mount + CA both apply.
    import src.loopholes as loopholes_mod

    state_root = tmp_path / "state"
    state_root.mkdir()
    state_dir = state_root / "has-jail"
    state_dir.mkdir()
    (state_dir / "ca.crt").write_text("ca")
    monkeypatch.setattr(loopholes_mod, "state_dir_for", lambda name: state_root / name)

    args = loopholes.docker_args_for(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    # Exactly two -v flags for this loophole: loophole dir + state dir.
    # No third file-mount-on-top for the CA.
    mount_sources = [a for a in args if ":/etc/yolo-jail/loopholes/has-jail" in a]
    assert len(mount_sources) == 1, f"expected 1 dir mount, got {mount_sources}"
    # NODE_EXTRA_CA_CERTS should point INTO the state-dir mount.
    node_ca = next(a for a in args if a.startswith("NODE_EXTRA_CA_CERTS="))
    assert node_ca == "NODE_EXTRA_CA_CERTS=/var/lib/yolo-jail/loopholes/has-jail/ca.crt"


def test_run_doctor_checks_missing_cmd(mods_dir: Path):
    mod = mods_dir / "missing"
    mod.mkdir()
    _write_manifest(
        mod,
        {
            "name": "missing",
            "description": "x",
            "doctor_cmd": ["/no/such/binary/anywhere"],
        },
    )
    results = loopholes.run_doctor_checks(
        loopholes.discover_loopholes(mods_dir, include_bundled=False)
    )
    assert results[0].returncode is None
    assert (
        "No such file" in results[0].output or "not found" in results[0].output.lower()
    )
