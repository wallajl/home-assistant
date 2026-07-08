# CodexBar add-on documentation

## What it does

- Downloads the selected CodexBar CLI release for your Home Assistant architecture.
- Runs `codexbar serve` on `127.0.0.1:8080`.
- Serves a Home Assistant Ingress dashboard through nginx on port `8099`.
- Persists CodexBar config under the add-on configuration mount.

## Configuration

Use `provider_config_json` to seed `/config/codexbar/config.json`. Leave it as `{}` if you want to manage the file manually in the add-on config directory.

Important paths inside the add-on:

- `CODEXBAR_CONFIG=/config/codexbar/config.json`
- `HOME=/config/home`
- Claude/Codex local data can be copied into `/config/.claude` and `/config/.codex` if you need local-cost scans.

## Security

The CodexBar API has no built-in authentication. This add-on intentionally exposes it only through Home Assistant Ingress; do not publish the internal nginx or CodexBar ports to the internet.
