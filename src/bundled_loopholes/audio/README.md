# audio loophole (bundled)

Ships with the yolo-jail wheel. Passes the user's PipeWire / PulseAudio socket through to the jail so microphone input and audio playback work inside the container — enabling Claude Code's `/voice` command and any other audio-dependent tooling.

## Activation

Gated on `requires.file_exists: ${XDG_RUNTIME_DIR}/pulse/native`. The loophole is *present* in every install but only *active* on Linux hosts where PipeWire or classic PulseAudio exposes its user socket at the standard path. This is the default on:

- PipeWire with `pipewire-pulse` (Fedora 34+, Ubuntu 22.04+, Arch, NixOS with the `pipewire.pulse.enable` flag).
- Classic PulseAudio (older distros, user-service mode).

To explicitly disable — e.g. you want the jail silent — add to `yolo-jail.jsonc`:

```jsonc
{
  "loopholes": {
    "audio": { "enabled": false }
  }
}
```

## macOS

Deliberately unsupported. Docker Desktop and Apple Container run Linux through a hypervisor VM with no CoreAudio passthrough, so there's no equivalent socket to bind. The `requires.file_exists` gate keeps the loophole inactive on macOS with no error noise. If you need voice features with yolo-jail-style isolation on macOS, run Claude Code directly on the host (the shared-credentials loophole keeps your jails and host session in sync).

## What gets wired up

When active:

- **Host bind-mount**: `$XDG_RUNTIME_DIR/pulse/native` → `/run/pulse/native` (read-write — audio frames flow both directions).
- **Env**: `PULSE_SERVER=unix:/run/pulse/native` inside the jail.

Any Pulse-compatible client inside the jail — `sox`, `ffmpeg -f pulse`, `parec`, `parecord`, Electron apps — finds and uses the socket automatically. No ALSA device passthrough, no `--group-add audio`, no Linux capabilities added. The jail inherits the user's existing Pulse permissions (you can already record; the container shares that trust through the socket).

## Verifying

```sh
# From inside a jail:
yolo loopholes status                # audio: active
env | grep PULSE_SERVER              # unix:/run/pulse/native
sox -d -n stat -v                    # prints microphone level
parecord --list-sources | head       # lists host audio sources
```

If `yolo loopholes status` reports audio as *inactive* on a Linux host that clearly has PipeWire running, confirm the socket path:

```sh
ls -l "${XDG_RUNTIME_DIR}/pulse/native"
```

Some minimal environments (headless servers, some SSH sessions) don't have `XDG_RUNTIME_DIR` set. Export it before running `yolo` if needed:

```sh
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
```
