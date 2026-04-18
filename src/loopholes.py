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

# Sources, ordered weakest → strongest: bundled < user < workspace.
SOURCE_BUNDLED = "bundled"
SOURCE_USER = "user"
SOURCE_CONFIG = "config"


def bundled_loopholes_dir() -> Path:
    """Loopholes that ship with the yolo-jail wheel — always available."""
    return Path(__file__).parent / "bundled_loopholes"


def user_loopholes_dir() -> Path:
    """Third-party loopholes installed by the user.  Override bundled on
    name collision."""
    return Path.home() / ".local" / "share" / "yolo-jail" / "loopholes"


def state_dir_for(name: str) -> Path:
    """Writable state directory for a loophole — CA files, leaf certs,
    locks, and anything else generated at runtime.  Bundled manifests
    live in the read-only wheel; their generated state lives here."""
    return Path.home() / ".local" / "share" / "yolo-jail" / "state" / name


# Kept for back-compat with older call sites.
loopholes_dir = user_loopholes_dir


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
class Requires:
    """Activation preconditions.  A loophole is *present* regardless —
    but it's only *active* (docker wiring, daemons spawned) when every
    ``requires`` predicate is satisfied.  Missing a requirement produces
    an informational 'inactive' state in ``yolo loopholes list`` but
    never an error."""

    # A binary that must be resolvable on the user's PATH.  Example:
    # ``"command_on_path": "claude"`` — no point running the Claude OAuth
    # broker if there's no Claude to refresh for.
    command_on_path: Optional[str] = None


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
    host_daemon: Optional[HostDaemon] = None
    jail_daemon: Optional[JailDaemon] = None
    requires: Requires = field(default_factory=Requires)
    # Where this loophole was loaded from: bundled / user / config.
    # Back-compat: from_config stays as a property below.
    source: str = SOURCE_USER

    @property
    def from_config(self) -> bool:
        """Back-compat alias — True when this loophole came from a
        yolo-jail.jsonc ``loopholes:`` entry (no manifest file)."""
        return self.source == SOURCE_CONFIG

    @property
    def has_ca(self) -> bool:
        return self.ca_cert is not None and self.ca_cert.is_file()

    @property
    def requirements_met(self) -> bool:
        req = self.requires
        if req.command_on_path is not None:
            import shutil as _shutil

            if _shutil.which(req.command_on_path) is None:
                return False
        return True

    @property
    def active(self) -> bool:
        """True when the loophole should actually be wired up this run.
        False for disabled loopholes OR loopholes whose ``requires``
        predicate isn't satisfied."""
        return self.enabled and self.requirements_met

    @property
    def inactive_reason(self) -> Optional[str]:
        """Human explanation for why an enabled loophole is inactive,
        or None if it's active.  Used by ``yolo loopholes list``."""
        if not self.enabled:
            return "disabled"
        req = self.requires
        if req.command_on_path is not None:
            import shutil as _shutil

            if _shutil.which(req.command_on_path) is None:
                return f"{req.command_on_path!r} not on PATH"
        return None

    @property
    def state_dir(self) -> Path:
        """Writable state directory for this loophole."""
        return state_dir_for(self.name)


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
    ca_cert_raw = data.get("ca_cert")
    if isinstance(ca_cert_raw, str) and ca_cert_raw:
        # {state} template → per-loophole state dir (writable).
        # Any other value is resolved relative to the manifest directory
        # (useful for loopholes that ship a pre-built CA in the bundled
        # or user dir).
        if "{state}" in ca_cert_raw:
            resolved = ca_cert_raw.replace("{state}", str(state_dir_for(name)))
            ca_cert = Path(resolved)
        else:
            ca_cert = (module_path / ca_cert_raw).resolve()

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
    requires = _parse_requires(manifest_path, data.get("requires"))

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
        requires=requires,
    )


def _parse_requires(manifest_path: Path, raw: Any) -> Requires:
    if raw is None:
        return Requires()
    if not isinstance(raw, dict):
        raise LoopholeError(f"{manifest_path}: 'requires' must be a mapping")
    command_on_path = raw.get("command_on_path")
    if command_on_path is not None and not isinstance(command_on_path, str):
        raise LoopholeError(
            f"{manifest_path}: 'requires.command_on_path' must be a string"
        )
    return Requires(command_on_path=command_on_path)


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
    """Surface yolo-jail.jsonc ``loopholes:`` entries as Loophole records.

    These are workspace-inline loopholes: no separate manifest file,
    the daemon lifecycle is spawned + unix-socket.
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
                source=SOURCE_CONFIG,
            )
        )
    return out


def _apply_workspace_overrides(
    existing: Dict[str, Loophole],
    loopholes_config: Optional[Dict[str, Any]],
) -> List[Loophole]:
    """Merge workspace ``loopholes:`` entries into already-loaded
    loopholes.  Returns (in document order) the list of NEW loopholes
    that didn't match anything existing — those become inline
    config-backed loopholes.

    Override merge rules (conservative — only the fields a workspace
    should be allowed to tweak):
      - ``enabled`` — replaces.
      - ``env`` — shallow-merges into ``host_daemon.env`` if present.
      - ``jail_env`` — shallow-merges into ``jail_env``.

    The command (and any other shape-defining fields) of a file-backed
    loophole are never overridden; workspaces ship config values, not
    surgery on the loophole's definition.
    """
    new_inline: List[Loophole] = []
    if not isinstance(loopholes_config, dict):
        return new_inline
    for name, spec in loopholes_config.items():
        if not isinstance(spec, dict):
            continue
        target = existing.get(str(name))
        if target is None:
            # No existing loophole by this name — fall back to inline
            # config-backed synthesis.
            new_inline.extend(_synthesize_config_loopholes({name: spec}))
            continue
        # Merge overrides in place.
        if "enabled" in spec:
            target.enabled = bool(spec["enabled"])
        env_override = spec.get("env") or {}
        if isinstance(env_override, dict) and target.host_daemon is not None:
            target.host_daemon.env = {
                **target.host_daemon.env,
                **{str(k): str(v) for k, v in env_override.items()},
            }
        jail_env_override = spec.get("jail_env") or {}
        if isinstance(jail_env_override, dict):
            target.jail_env = {
                **target.jail_env,
                **{str(k): str(v) for k, v in jail_env_override.items()},
            }
    return new_inline


def _load_from_dir(dir_path: Path, source: str) -> Dict[str, Loophole]:
    """Scan ``dir_path`` for loophole manifests.  Returns a name→Loophole
    mapping.  Invalid manifests are skipped silently; use
    ``validate_loopholes`` for diagnostics.
    """
    out: Dict[str, Loophole] = {}
    if not dir_path.is_dir():
        return out
    for child in sorted(dir_path.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            loophole = _load_manifest(child)
        except LoopholeError:
            continue
        loophole.source = source
        out[loophole.name] = loophole
    return out


def discover_loopholes(
    root: Optional[Path] = None,
    *,
    include_disabled: bool = False,
    loopholes_config: Optional[Dict[str, Any]] = None,
    include_bundled: bool = True,
) -> List[Loophole]:
    """Return every loophole visible at run time, merged across the three
    sources (bundled < user < workspace overrides) and with workspace
    inline definitions appended.

    ``root`` — override the user-level loopholes directory.  When None,
    uses ``user_loopholes_dir()``.  Bundled dir is always added unless
    ``include_bundled=False`` (used by tests that want an isolated view).

    Merge semantics on name collision:
      - user overrides bundled (the whole manifest — different dir, so
        it's a full replacement).
      - workspace config overrides either, but only on a small set of
        safe fields (enabled, env, jail_env).  See
        ``_apply_workspace_overrides``.
    """
    root = root or user_loopholes_dir()
    by_name: Dict[str, Loophole] = {}
    if include_bundled:
        by_name.update(_load_from_dir(bundled_loopholes_dir(), SOURCE_BUNDLED))
    by_name.update(_load_from_dir(root, SOURCE_USER))
    inline = _apply_workspace_overrides(by_name, loopholes_config)

    out: List[Loophole] = []
    for m in by_name.values():
        if not include_disabled and not m.enabled:
            continue
        out.append(m)
    for m in inline:
        if not include_disabled and not m.enabled:
            continue
        out.append(m)
    return out


def validate_loopholes(
    root: Optional[Path] = None,
    *,
    include_bundled: bool = True,
) -> List["tuple[Path, Optional[Loophole], Optional[str]]"]:
    """Return one entry per file-backed loophole directory (bundled + user).

    Config-synthesized loopholes are not included — they have no manifest
    to validate (they live in yolo-jail.jsonc).
    """
    out: List["tuple[Path, Optional[Loophole], Optional[str]]"] = []
    dirs: List["tuple[Path, str]"] = []
    if include_bundled:
        dirs.append((bundled_loopholes_dir(), SOURCE_BUNDLED))
    dirs.append(((root or user_loopholes_dir()), SOURCE_USER))
    for dir_path, source in dirs:
        if not dir_path.is_dir():
            continue
        for child in sorted(dir_path.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                loophole = _load_manifest(child)
                loophole.source = source
                out.append((child, loophole, None))
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
        # Config-backed loopholes: wiring lives in cli.start_loopholes.
        if m.from_config:
            continue
        # Present-but-inactive loopholes (disabled, or requires not met)
        # emit nothing — they show up in ``yolo loopholes list`` but are
        # not wired into the jail.
        if not m.active:
            continue
        container_dir = f"/etc/yolo-jail/loopholes/{m.name}"

        for intercept in m.intercepts:
            args.extend(["--add-host", f"{intercept.host}:{m.broker_ip}"])

        # Track which container paths are already covered by a directory
        # mount so we can point NODE_EXTRA_CA_CERTS at the CA without
        # trying to overlay a file on top of a dir mount (podman rejects
        # that with a cryptic "conmon bytes" container-create error).
        state_container = f"/var/lib/yolo-jail/loopholes/{m.name}"
        state_mounted = False
        dir_mounted = False

        if m.jail_daemon is not None:
            # Mount the bundled/user loophole dir for helper files
            # (jail.py, etc.).
            args.extend(["-v", f"{m.path}:{container_dir}:ro"])
            dir_mounted = True
            # State dir — writable on host, read-only in jail.  Lets
            # daemons inside the jail read generated files (leaf cert,
            # etc.) without rebaking them into the read-only bundled
            # dir.  Only mount if it exists (otherwise podman errors).
            if m.state_dir.is_dir():
                args.extend(["-v", f"{m.state_dir}:{state_container}:ro"])
                state_mounted = True

        # Make the CA reachable inside the jail.  If it's already inside
        # a dir we mounted (state or loophole), compute the container
        # path from that existing mount — don't stack a file mount on
        # top of a dir mount, podman refuses.  Fall back to an explicit
        # file mount only when the CA lives somewhere unrelated.
        if m.has_ca and m.ca_cert is not None:
            container_ca: Optional[str] = None
            if state_mounted:
                try:
                    rel = m.ca_cert.relative_to(m.state_dir)
                    container_ca = f"{state_container}/{rel}"
                except ValueError:
                    pass
            if container_ca is None and dir_mounted:
                try:
                    rel = m.ca_cert.relative_to(m.path)
                    container_ca = f"{container_dir}/{rel}"
                except ValueError:
                    pass
            if container_ca is None:
                container_ca = f"{container_dir}/ca.crt"
                args.extend(["-v", f"{m.ca_cert}:{container_ca}:ro"])
            trusted_ca_paths.append(container_ca)

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
        if not m.active:
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
