# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in YOLO Jail, please report it responsibly.

**Email:** mschulkind@gmail.com

Please include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

## Response Timeline

- **Acknowledgment:** Within 48 hours
- **Initial assessment:** Within 1 week
- **Fix timeline:** Depends on severity, typically within 2 weeks for critical issues

## Scope

YOLO Jail is a container isolation tool. Security-relevant areas include:

- **Container escape** — the jail must not allow access to host credentials, SSH keys, or cloud tokens
- **Config injection** — agents must not be able to silently modify `yolo-jail.jsonc` (config safety approval prevents this)
- **Shim bypass** — blocked tools must not be circumvable within the jail
- **Identity isolation** — the jail's `gh auth` and `gemini auth` must be separate from the host
- **Mount safety** — extra mounts must be read-only by default
- **Network isolation** — bridge mode must properly isolate container networking

### Out of Scope

- Vulnerabilities in Docker/Podman themselves
- Issues requiring root access on the host
- Agents intentionally bypassing shims with `YOLO_BYPASS_SHIMS=1` (this is a documented override)

## Supported Versions

Only the latest version on the `main` branch is supported with security fixes. There are no LTS releases at this time.

## Disclosure Policy

We follow coordinated disclosure. Please allow reasonable time for a fix before public disclosure.
