"""yolo-jail loopholes — unified registry for host↔jail integrations.

A **loophole** is a single controlled permeability point between the jail
and the host: the jail talks to something through the loophole, and
nothing escapes that's not declared.  Examples:

- ``claude-oauth-broker`` — MITM proxy that serializes Claude OAuth refreshes
  (transport: ``tls-intercept``, lifecycle: ``external``).
- ``host-processes`` — read-only allowlisted view of host processes
  (transport: ``unix-socket``, lifecycle: ``spawned``).
- ``llm-audit`` (hypothetical third-party) — logs every inference request
  (transport: ``tls-intercept``, lifecycle: ``external``).

A loophole lives under ``~/.local/share/yolo-jail/loopholes/<name>/`` with
at least a ``manifest.jsonc``.  The loader discovers every installed
loophole at jail startup and applies its declared wiring — CA cert mount,
DNS overrides, socket bind mount, jail env — to the docker run command.

``loopholes`` config entries in ``yolo-jail.jsonc`` are the second
source of loopholes (workspace-scoped, unix-socket + spawned).  The
loader surfaces them alongside file-backed loopholes so
``yolo loopholes list`` is the single pane of glass.  ``start_loopholes``
(in ``cli.py``) owns their process lifecycle; this registry is
discovery and doctor-integration only.

Manifest schema (v1)
--------------------

.. code-block:: jsonc

    {
      "name": "claude-oauth-broker",            // required, must match dir name
      "description": "…",                        // required
      "version": 1,                              // manifest format version
      "enabled": true,                           // default true
      "transport": "tls-intercept",              // or "unix-socket" or "none"
      "lifecycle": "external",                   // or "spawned"
      "intercepts": [                            // DNS override inside the jail
        {"host": "platform.claude.com"}
      ],
      "broker_ip": "127.0.0.1",                  // where the intercept points;
                                                  // use "127.0.0.1" to route to
                                                  // a jail-side daemon, or
                                                  // "host-gateway" for a
                                                  // host-side one.
      "ca_cert": "ca.crt",                       // auto-mounted + trusted
      "jail_env": {"FOO": "bar"},                // any transport
      "doctor_cmd": ["bin-name", "--self-check"],// optional health check

      // A daemon to spawn on the HOST per jail boot.  Launched by
      // ``start_loopholes``, torn down on jail exit.  The loophole's
      // socket is bind-mounted into the jail; the daemon's cmd
      // receives the host-side socket path as ``{socket}``.
      "host_daemon": {
        "cmd": ["yolo-claude-oauth-broker-host", "--socket", "{socket}"],
        "env": {"MY_VAR": "val"}
      },

      // A daemon to spawn INSIDE the jail at boot.  Supervised by the
      // jail-side supervisor (src/jail_daemon_supervisor.py): restarted
      // per ``restart`` policy until PID 1 exits.
      "jail_daemon": {
        "cmd": ["python3", "/etc/yolo-jail/loopholes/<name>/jail.py"],
        "restart": "on-failure"  // "always" | "on-failure" | "no"
      }
    }

The loader is stateless — every jail boot re-reads manifests.  Disabling
a loophole is a one-line edit (``"enabled": false``) and takes effect on
the next ``yolo run``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pyjson5


# Both podman and docker translate the literal "host-gateway" into the
# right host-reachable-from-container address for the active runtime
# (pasta tunnel, CNI bridge, Docker Desktop VM gateway, …). Hardcoding a
# specific IP like 169.254.1.2 only works on one runtime/config combination.
DEFAULT_BROKER_IP = "host-gateway"

VALID_TRANSPORTS = {"tls-intercept", "unix-socket", "none"}
VALID_LIFECYCLES = {"external", "spawned"}
VALID_RESTART_POLICIES = {"always", "on-failure", "no"}


def loopholes_dir() -> Path:
    """Return the host-side loopholes directory."""
    return Path.home() / ".local" / "share" / "yolo-jail" / "loopholes"


@dataclass
class Intercept:
    host: str


@dataclass
class JailDaemon:
    """A daemon the jail-side supervisor starts + restarts at boot."""

    cmd: List[str]
    restart: str = "on-failure"


@dataclass
class HostDaemon:
    """A host-side daemon spawned by ``start_loopholes`` per jail boot."""

    cmd: List[str]
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class Loophole:
    """A loaded, validated loophole manifest."""

    name: str
    description: str
    path: Path
    enabled: bool = True
    transport: str = "tls-intercept"
    lifecycle: str = "external"
    intercepts: List[Intercept] = field(default_factory=list)
    broker_ip: str = DEFAULT_BROKER_IP
    ca_cert: Optional[Path] = None
    jail_env: Dict[str, str] = field(default_factory=dict)
    doctor_cmd: Optional[List[str]] = None
    # Daemons described by the manifest itself (see JailDaemon / HostDaemon
    # dataclasses).  Either may be absent.
    host_daemon: Optional[HostDaemon] = None
    jail_daemon: Optional[JailDaemon] = None
    # True for loopholes synthesized from yolo-jail.jsonc's ``loopholes``
    # config block (workspace-scoped, spawned + unix-socket).  False for
    # file-backed loopholes with their own manifest.jsonc under
    # ``~/.local/share/yolo-jail/loopholes/``.  Used to decide which
    # integration path applies: config-backed loopholes route through
    # ``start_loopholes`` in cli.py; file-backed ones through
    # ``docker_args_for`` below.
    from_config: bool = False

    @property
    def has_ca(self) -> bool:
        return self.ca_cert is not None and self.ca_cert.is_file()


class LoopholeError(ValueError):
    """Raised when a manifest is malformed."""


def _load_manifest(module_path: Path) -> Loophole:
    manifest_path = module_path / "manifest.jsonc"
    if not manifest_path.is_file():
        raise LoopholeError(f"{manifest_path} not found")

    try:
        data: Dict[str, Any] = pyjson5.loads(manifest_path.read_text())
    except (OSError, ValueError, pyjson5.Json5Exception) as e:
        raise LoopholeError(f"{manifest_path}: {e}") from e

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise LoopholeError(f"{manifest_path}: 'name' is required")
    if name != module_path.name:
        raise LoopholeError(
            f"{manifest_path}: name='{name}' disagrees with directory "
            f"'{module_path.name}' — they must match"
        )

    description = data.get("description", "")
    if not isinstance(description, str):
        raise LoopholeError(f"{manifest_path}: 'description' must be a string")

    transport = str(data.get("transport", "tls-intercept"))
    if transport not in VALID_TRANSPORTS:
        raise LoopholeError(
            f"{manifest_path}: transport={transport!r} not in {sorted(VALID_TRANSPORTS)}"
        )

    lifecycle = str(data.get("lifecycle", "external"))
    if lifecycle not in VALID_LIFECYCLES:
        raise LoopholeError(
            f"{manifest_path}: lifecycle={lifecycle!r} not in {sorted(VALID_LIFECYCLES)}"
        )

    intercepts_raw = data.get("intercepts") or []
    if not isinstance(intercepts_raw, list):
        raise LoopholeError(f"{manifest_path}: 'intercepts' must be a list")
    intercepts: List[Intercept] = []
    for entry in intercepts_raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("host"), str):
            raise LoopholeError(
                f"{manifest_path}: each intercept needs a string 'host'"
            )
        intercepts.append(Intercept(host=entry["host"]))

    ca_cert: Optional[Path] = None
    ca_cert_rel = data.get("ca_cert")
    if isinstance(ca_cert_rel, str) and ca_cert_rel:
        ca_cert = (module_path / ca_cert_rel).resolve()

    jail_env_raw = data.get("jail_env") or {}
    if not isinstance(jail_env_raw, dict):
        raise LoopholeError(f"{manifest_path}: 'jail_env' must be a mapping")
    jail_env = {str(k): str(v) for k, v in jail_env_raw.items()}

    doctor_cmd_raw = data.get("doctor_cmd")
    doctor_cmd: Optional[List[str]] = None
    if doctor_cmd_raw is not None:
        if not isinstance(doctor_cmd_raw, list) or not all(
            isinstance(x, str) for x in doctor_cmd_raw
        ):
            raise LoopholeError(
                f"{manifest_path}: 'doctor_cmd' must be a list of strings"
            )
        doctor_cmd = list(doctor_cmd_raw)

    host_daemon = _parse_host_daemon(manifest_path, data.get("host_daemon"))
    jail_daemon = _parse_jail_daemon(manifest_path, data.get("jail_daemon"))

    return Loophole(
        name=name,
        description=description,
        path=module_path,
        enabled=bool(data.get("enabled", True)),
        transport=transport,
        lifecycle=lifecycle,
        intercepts=intercepts,
        broker_ip=str(data.get("broker_ip") or DEFAULT_BROKER_IP),
        ca_cert=ca_cert,
        jail_env=jail_env,
        doctor_cmd=doctor_cmd,
        host_daemon=host_daemon,
        jail_daemon=jail_daemon,
    )


def _parse_host_daemon(manifest_path: Path, raw: Any) -> Optional[HostDaemon]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LoopholeError(f"{manifest_path}: 'host_daemon' must be a mapping")
    cmd = raw.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise LoopholeError(
            f"{manifest_path}: 'host_daemon.cmd' must be a non-empty list of strings"
        )
    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise LoopholeError(f"{manifest_path}: 'host_daemon.env' must be a mapping")
    return HostDaemon(
        cmd=list(cmd),
        env={str(k): str(v) for k, v in env_raw.items()},
    )


def _parse_jail_daemon(manifest_path: Path, raw: Any) -> Optional[JailDaemon]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LoopholeError(f"{manifest_path}: 'jail_daemon' must be a mapping")
    cmd = raw.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise LoopholeError(
            f"{manifest_path}: 'jail_daemon.cmd' must be a non-empty list of strings"
        )
    restart = str(raw.get("restart", "on-failure"))
    if restart not in VALID_RESTART_POLICIES:
        raise LoopholeError(
            f"{manifest_path}: 'jail_daemon.restart' not in {sorted(VALID_RESTART_POLICIES)}"
        )
    return JailDaemon(cmd=list(cmd), restart=restart)


def _synthesize_config_loopholes(
    loopholes_config: Optional[Dict[str, Any]],
) -> List[Loophole]:
    """Surface entries from ``yolo-jail.jsonc``'s ``loopholes`` block as
    read-only Loophole records.

    These are not file-backed (no manifest.jsonc) and can't be enabled /
    disabled through the loopholes CLI — you edit yolo-jail.jsonc.  They
    appear in ``yolo loopholes list`` so the operator has a single pane
    of glass on what's crossing the jail boundary.
    """
    out: List[Loophole] = []
    if not isinstance(loopholes_config, dict):
        return out
    for name, spec in loopholes_config.items():
        if not isinstance(spec, dict):
            continue
        description = str(spec.get("description") or "")
        enabled = bool(spec.get("enabled", True))
        doctor = spec.get("doctor_cmd")
        doctor_cmd = (
            list(doctor)
            if isinstance(doctor, list) and all(isinstance(x, str) for x in doctor)
            else None
        )
        out.append(
            Loophole(
                name=str(name),
                description=description,
                path=Path(f"<yolo-jail.jsonc:loopholes.{name}>"),
                enabled=enabled,
                transport="unix-socket",
                lifecycle="spawned",
                doctor_cmd=doctor_cmd,
                from_config=True,
            )
        )
    return out


def discover_loopholes(
    root: Optional[Path] = None,
    *,
    include_disabled: bool = False,
    loopholes_config: Optional[Dict[str, Any]] = None,
) -> List[Loophole]:
    """Return every validated loophole — file-backed plus any synthesized
    from the ``loopholes`` block in yolo-jail.jsonc.

    Invalid manifests are skipped silently — a broken third-party loophole
    should not prevent ``yolo run`` from starting.  Use ``validate_loopholes``
    for operator-facing diagnostics.
    """
    root = root or loopholes_dir()
    out: List[Loophole] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                loophole = _load_manifest(child)
            except LoopholeError:
                continue
            if not include_disabled and not loophole.enabled:
                continue
            out.append(loophole)
    for synth in _synthesize_config_loopholes(loopholes_config):
        if not include_disabled and not synth.enabled:
            continue
        out.append(synth)
    return out


def validate_loopholes(
    root: Optional[Path] = None,
) -> List["tuple[Path, Optional[Loophole], Optional[str]]"]:
    """Return one entry per file-backed loophole directory.

    Config-synthesized loopholes are not included — they have no manifest
    to validate (they live in yolo-jail.jsonc).
    """
    root = root or loopholes_dir()
    if not root.is_dir():
        return []
    out: List["tuple[Path, Optional[Loophole], Optional[str]]"] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            out.append((child, _load_manifest(child), None))
        except LoopholeError as e:
            out.append((child, None, str(e)))
    return out


# ---------------------------------------------------------------------------
# docker run integration — only file-backed tls-intercept loopholes.
# Config-backed (spawned + unix-socket) loopholes ride the ``start_loopholes``
# pipeline in cli.py.
# ---------------------------------------------------------------------------


def docker_args_for(loopholes: List[Loophole]) -> List[str]:
    """Translate file-backed loopholes into docker run flags.

    Emits --add-host, CA mounts, NODE_EXTRA_CA_CERTS, jail_env, plus the
    YOLO_JAIL_DAEMONS payload and a full loophole-dir bind mount when a
    loophole declares a ``jail_daemon``.

    Config-backed loopholes are ignored here — their wiring happens in
    ``cli.start_loopholes``.  Idempotent and side-effect free.
    """
    import json as _json

    args: List[str] = []
    trusted_ca_paths: List[str] = []
    jail_daemons_payload: List[Dict[str, Any]] = []
    for m in loopholes:
        if m.from_config:
            continue
        container_dir = f"/etc/yolo-jail/loopholes/{m.name}"

        for intercept in m.intercepts:
            args.extend(["--add-host", f"{intercept.host}:{m.broker_ip}"])

        # If the loophole has a jail_daemon, mount the whole loophole dir
        # so the daemon cmd can reference files shipped with it (e.g. a
        # jail.py proxy).  Otherwise fall back to mounting just ca.crt.
        if m.jail_daemon is not None:
            args.extend(["-v", f"{m.path}:{container_dir}:ro"])
            if m.has_ca and m.ca_cert is not None:
                trusted_ca_paths.append(f"{container_dir}/{m.ca_cert.name}")
        elif m.has_ca:
            container_path = f"{container_dir}/ca.crt"
            args.extend(["-v", f"{m.ca_cert}:{container_path}:ro"])
            trusted_ca_paths.append(container_path)

        if m.jail_daemon is not None:
            jail_daemons_payload.append(
                {
                    "name": m.name,
                    "cmd": list(m.jail_daemon.cmd),
                    "restart": m.jail_daemon.restart,
                }
            )

        for k, v in m.jail_env.items():
            args.extend(["-e", f"{k}={v}"])

    if trusted_ca_paths:
        args.extend(["-e", f"NODE_EXTRA_CA_CERTS={os.pathsep.join(trusted_ca_paths)}"])
    if jail_daemons_payload:
        args.extend(["-e", f"YOLO_JAIL_DAEMONS={_json.dumps(jail_daemons_payload)}"])
    return args


def manifest_host_daemon_specs(loopholes: List[Loophole]) -> Dict[str, Dict[str, Any]]:
    """Return ``{name: spec}`` for every file-backed loophole with a
    ``host_daemon``, shaped like the ``loopholes:`` config block so
    ``cli.start_loopholes`` can spawn them through the existing pipeline.

    ``{socket}`` stays as a placeholder; the spawner substitutes the real
    per-jail socket path when it creates the listener.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for m in loopholes:
        if m.from_config or m.host_daemon is None:
            continue
        spec: Dict[str, Any] = {
            "command": list(m.host_daemon.cmd),
            "description": m.description,
        }
        if m.host_daemon.env:
            spec["env"] = dict(m.host_daemon.env)
        out[m.name] = spec
    return out


# ---------------------------------------------------------------------------
# doctor integration
# ---------------------------------------------------------------------------


@dataclass
class DoctorResult:
    loophole: Loophole
    returncode: Optional[int]  # None if doctor_cmd absent or could not run
    output: str


def run_doctor_checks(
    loopholes: List[Loophole], *, timeout: float = 10.0
) -> List[DoctorResult]:
    """Execute each loophole's ``doctor_cmd`` and collect results."""
    results: List[DoctorResult] = []
    for m in loopholes:
        if not m.doctor_cmd:
            results.append(DoctorResult(loophole=m, returncode=None, output=""))
            continue
        try:
            proc = subprocess.run(
                m.doctor_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (proc.stdout or proc.stderr).strip()
            results.append(
                DoctorResult(loophole=m, returncode=proc.returncode, output=output)
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            results.append(DoctorResult(loophole=m, returncode=None, output=str(e)))
    return results


# ---------------------------------------------------------------------------
# enable / disable — file-backed loopholes only
# ---------------------------------------------------------------------------


def set_enabled(module_path: Path, enabled: bool) -> None:
    """Toggle ``enabled`` in a loophole's manifest without disturbing other keys."""
    manifest_path = module_path / "manifest.jsonc"
    text = manifest_path.read_text()
    data = pyjson5.loads(text)
    data["enabled"] = bool(enabled)
    import json as _json

    header = (
        "// yolo-jail loophole manifest. See src/loopholes.py for schema.\n"
        "// 'enabled' toggled via `yolo loopholes {enable,disable}`.\n"
    )
    manifest_path.write_text(header + _json.dumps(data, indent=2) + "\n")
