# Research: Chrome DevTools MCP in Docker/Nix

**Date:** 2026-02-16  
**Status:** Completed

## Objective
Find why `chrome-devtools-mcp` fails in jail with `Protocol error (Target.setDiscoverTargets): Target closed`, then validate a fix with a real HN `new_page` + `take_snapshot` run.

## Progressive Log

### 07:05 UTC - Reproduced failure in jail-like container
- Ran Copilot non-interactive in the jail image with current MCP args.
- `chrome-devtools-new_page` failed with:
  - `Protocol error (Target.setDiscoverTargets): Target closed`
- Log confirmed MCP server started, then tool call failed.

### 07:08 UTC - Read upstream docs/issues
- Read upstream troubleshooting docs:
  - `chrome-devtools-mcp` troubleshooting recommends log file + `DEBUG=*`.
  - `Target closed` generally means browser launch/connect failure.
- Read issue threads (`#99`, `#281`, `#503`) and comments:
  - `Target closed` can hide underlying browser startup issues.
  - Workarounds often involve isolated profiles and explicit browser config.

### 07:12 UTC - Source-level analysis in installed MCP package
- Read `build/src/main.js` and `build/src/browser.js` from installed `chrome-devtools-mcp@0.17.0`.
- Key findings:
  - Launch path uses Puppeteer with `pipe: true`.
  - Without `--isolated`, MCP reuses persistent profile under `~/.cache/chrome-devtools-mcp/chrome-profile`.
  - `StdioServerTransport` in current build uses **newline-delimited JSON**, not old `Content-Length` framing.

### 07:16 UTC - Isolated launch behavior
- Raw Puppeteer launch with persistent `userDataDir` reproduced immediate:
  - `Protocol error (Target.setDiscoverTargets): Target closed`
- Raw Puppeteer launch with isolated temp profile avoided immediate launch crash.

### 07:20 UTC - Fontconfig/Nix investigation
- Running `fc-list` inside image showed:
  - `Fontconfig error: Cannot load default config file: No such file: (null)`
  - `/etc/fonts` missing in image.
- Browser stderr on failing pages showed repeated HarfBuzz text errors (`render_text_harfbuzz.cc`).

### 07:23 UTC - Confirmed runtime symptom on complex pages
- Raw Puppeteer tests:
  - `example.com` worked.
  - `news.ycombinator.com` and `github.com` timed out on `Runtime.callFunctionOn`.
- This matched observed MCP snapshot/evaluate hangs.

### 07:25 UTC - Validated fontconfig fix experimentally
- Set:
  - `FONTCONFIG_FILE=/nix/store/...-fontconfig.../etc/fonts/fonts.conf`
  - `FONTCONFIG_PATH=/nix/store/...-fontconfig.../etc/fonts`
- Re-ran Puppeteer tests:
  - HN and GitHub title/evaluate calls succeeded.

### 07:28 UTC - Validated full MCP HN flow
- Ran direct MCP stdio client (newline protocol) with:
  - `--isolated`
  - fontconfig env set
- Result:
  - `new_page_isError None seconds 0.69`
  - `take_snapshot_isError None seconds 0.069`
  - `snapshot_contains_hn True chars 39430`

### 07:32 UTC - Applied repository changes
- Updated MCP config generation to include `--isolated` for chrome-devtools (Copilot + Gemini).
- Updated wrappers to export fontconfig defaults.
- Updated image definition to provide `/etc/fonts` and set fontconfig env defaults.

### 07:35 UTC - Validation
- Test suite:
  - `11 passed, 1 skipped`

### Thinking & Synthesis
- The failure had **two separate causes**:
  1. **Persistent profile launch instability** in this jail setup (`Target closed`), fixed by `--isolated`.
  2. **Missing fontconfig defaults** in Nix image causing renderer/text path instability on complex pages (`Runtime.callFunctionOn` timeouts), fixed by providing `/etc/fonts` and `FONTCONFIG_*`.
- GPU and Chromium flags were not the primary issue. The reproducible turning points were profile isolation and fontconfig correctness.

## Final Synthesis / Conclusions
- Root cause is not one bug but a launch/runtime combination:
  - non-isolated profile + Docker/Nix env caused browser target close,
  - missing default fontconfig caused runtime evaluate/snapshot hangs.
- Reliable fix requires both:
  - `--isolated` in chrome-devtools MCP args,
  - consistent fontconfig defaults in image/wrappers.
- HN MCP flow (`new_page` + `take_snapshot`) is demonstrably successful with these conditions.

## Open Questions
- [ ] Should we keep `--disable-dev-shm-usage` now that jail already sets `--shm-size=2g`, or drop it to reduce disk-backed shared-memory pressure?

## Answered Questions (Memorialized)
- **Q**: Is this a GPU problem?  
  **A**: No. The blockers were profile isolation and fontconfig setup, not GPU acceleration.

## References
- [Local File](../../src/entrypoint.sh)
- [Local File](../../flake.nix)
- [Web Link](https://raw.githubusercontent.com/ChromeDevTools/chrome-devtools-mcp/main/docs/troubleshooting.md)
- [Web Link](https://raw.githubusercontent.com/puppeteer/puppeteer/main/docs/troubleshooting.md)
- [Web Link](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/99)
- [Web Link](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/281)
- [Web Link](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/503)
