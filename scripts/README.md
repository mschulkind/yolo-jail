# yolo-jail scripts

Host-side scripts that support the jail. These run on the host, not inside any jail.

## Claude token refresher — removed

Historically a systemd `--user` timer refreshed the shared Claude OAuth token every 10 minutes so jails never refreshed on their own. That daemon is gone: the `claude-oauth-broker` loophole now refreshes on demand, inside its flock, the first time a jail asks. Same race-avoidance guarantee with one less moving part.

If you upgraded from a pre-broker install, `just deploy` automatically disables and removes the legacy `claude-token-refresher.service` / `claude-token-refresher.timer` units. See [docs/claude-token-logouts.md](../docs/claude-token-logouts.md) for the post-broker troubleshooting walkthrough.
