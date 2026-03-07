# Open Source Checklist — yolo_jail

Pre-flight checklist before making the public repo live. Modeled on the vantage open-source workflow.

---

## 0. Name Decision

- [ ] **Pick a name** — See `docs/NAME_CANDIDATES.md` for 50 candidates with availability
- [ ] **Register PyPI name** — Upload placeholder or initial release
- [ ] **Create GitHub repos** — `mschulkind/<name>` (public) + `mschulkind/<name>-private` (private)
- [ ] **Update all references** — pyproject.toml, README, imports, CLI entry point, flake.nix

## 1. Legal & Licensing

- [ ] **License file** — Apache 2.0, `LICENSE` at repo root
- [ ] **NOTICE file** — `NOTICE` at repo root
- [ ] **Dependency license audit** — All Python deps compatible with Apache 2.0
  - pyjson5: Apache 2.0 ✅
  - typer: MIT ✅
  - rich: MIT ✅
  - pytest: MIT ✅
- [ ] **CLA / DCO** — Decide: not requiring, or DCO sign-off

## 2. Sensitive Content

- [ ] **Secrets scan** — Run gitleaks or manual scan on all public files
- [ ] **Hardcoded paths** — No `/home/matt/` or internal paths in public code
- [ ] **Personal config** — Ensure `yolo-jail.jsonc` example is generic, not personal
- [ ] **AGENTS.md** — Private only (never in public repo)
- [ ] **OPEN_QUESTIONS.md** — Private only
- [ ] **Review all docs/** — Ensure nothing leaks internal infrastructure

## 3. Repository Setup (jj + Two Remotes)

- [ ] **Install jj** — `mise install jj` or via nix
- [ ] **Initialize jj** — `jj init --git-repo .` (colocated with git)
- [ ] **Create bookmark structure** — `main` → `staging` → `dev`
- [ ] **Add remotes** — `public` (GitHub public) + `private` (GitHub private)
- [ ] **Create `scripts/public-files.txt`** — Define what's public vs private
- [ ] **First push** — Push `main` to both remotes, `dev`+`staging` to private only
- [ ] **Set immutability** — `jj config set --repo revsets.immutable_heads 'builtin_immutable_heads()'`
- [ ] **Rename git remote** — Rename `origin` to `private`, add `public`

## 4. Repository Hygiene

- [ ] **README quality** — Clear install instructions, usage examples, feature list, for strangers
- [ ] **CONTRIBUTING.md** — Dev setup, coding standards, PR process
- [ ] **CODE_OF_CONDUCT.md** — Contributor Covenant v2.1
- [ ] **SECURITY.md** — Responsible disclosure policy
- [ ] **Issue templates** — `.github/ISSUE_TEMPLATE/` bug report + feature request
- [ ] **PR template** — `.github/pull_request_template.md`

## 5. CI/CD

- [ ] **GitHub Actions** — `.github/workflows/ci.yml` — lint, test
- [ ] **Badge in README** — CI status badge
- [ ] **Dependabot** — `.github/dependabot.yml` for pip + actions
- [ ] **Gitleaks** — Secrets scanning in CI

## 6. Build & Install Verification

- [ ] **Clean clone test** — `uv sync && just test` from fresh clone
- [ ] **pip installable** — `pip install -e .` works
- [ ] **Entry point works** — `yolo --help` after install
- [ ] **Nix build** — `nix build .#dockerImage` works from clean checkout
- [ ] **Python version** — Document minimum (3.13) in pyproject.toml and README
- [ ] **System deps** — Document required system packages (nix, docker/podman)
- [ ] **Container runtime** — Test with both Docker and Podman

## 7. Documentation

- [ ] **README** — Update for public audience (clear install, strangers-friendly language)
- [ ] **User guide** — Create docs/USER_GUIDE.md with setup walkthrough
- [ ] **New user guide** — First-run experience, `gh auth login`, `gemini login` inside jail
- [ ] **Configuration reference** — Document `yolo-jail.jsonc` fully (already in `yolo config-ref`)
- [ ] **Architecture docs** — Review `docs/` for private leaks
- [ ] **Doctor command** — `yolo doctor` validates environment

## 8. Code Readiness

- [ ] **Doctor command** — `yolo doctor` for environment health checks
- [ ] **Error messages** — User-friendly, not developer-debug-oriented
- [ ] **No personal defaults** — All config should work for any user
- [ ] **Help text** — `yolo --help` and all subcommands have clear help text
- [ ] **Config-ref** — `yolo config-ref` is comprehensive and up to date

## 9. Branding & Presentation

- [ ] **GitHub repo settings** — After push:
  ```bash
  gh repo edit mschulkind/<name> \
    --description "Secure container jail for AI agents — run Copilot and Gemini in YOLO mode safely" \
    --add-topic ai,agents,docker,podman,nix,security,sandbox,copilot,gemini
  ```
- [ ] **Release** — After push:
  ```bash
  gh release create v0.1.0 --repo mschulkind/<name> \
    --title "v0.1.0 — Initial Release" \
    --generate-notes
  ```

## 10. Post-Launch

- [ ] **Verify staging workflow** — Full public release cycle works end-to-end
- [ ] **Monitor** — Watch for issues, stars, forks
- [ ] **PyPI publishing** — Publish to PyPI once name is decided
- [ ] **Announce** — Reddit r/LocalLLaMA, r/commandline, Hacker News

---

## What Goes Public

| Path | Notes |
|------|-------|
| `src/cli.py` | Host-side CLI |
| `src/entrypoint.py` | Container-side startup |
| `src/shims/` | Blocked tool shim scripts |
| `src/__init__.py` | Package init |
| `tests/` | All tests |
| `pyproject.toml` | Package config |
| `Justfile` | Task runner (public targets only) |
| `flake.nix` | Nix image definition |
| `flake.lock` | Nix lock file |
| `mise.toml` | Tool versions |
| `uv.lock` | Lock file |
| `README.md` | Public readme |
| `LICENSE` | License file |
| `NOTICE` | Attribution |
| `CONTRIBUTING.md` | Contributor guide |
| `CODE_OF_CONDUCT.md` | Contributor Covenant |
| `SECURITY.md` | Disclosure policy |
| `.github/` | CI + templates |
| `docs/config-safety.md` | Config change approval docs |
| `docs/storage-and-config.md` | Storage hierarchy docs |

## What Stays Private

| Path | Reason |
|------|--------|
| `AGENTS.md` | Agent config and infra details |
| `OPEN_QUESTIONS.md` | Internal dev decisions |
| `.copilot/` | Agent skills and config |
| `.yolo/` | Jail state |
| `yolo-jail.jsonc` | Personal jail config |
| `yolo-enter.sh` | Personal entry point wrapper |
| `docs/NAME_CANDIDATES.md` | This naming doc |
| `docs/OPEN_SOURCE_CHECKLIST.md` | This checklist |
| `docs/plans/` | Internal planning |
| `docs/research/` | Internal research notes |
| `docs/tasks/` | Internal task tracking |
| `docs/kitty-jail.conf` | Personal kitty config |
| `scratch/` | Scratch files |
| `result` | Nix build output symlink |
