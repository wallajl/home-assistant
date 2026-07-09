# Changelog

## 0.3.2

- Make setup UI API calls resolve correctly when Home Assistant opens the add-on through an ingress menu URL without a trailing slash.

## 0.3.1

- Strip terminal ANSI escape codes from Codex/Claude login output so the displayed URL and one-time code are readable and the Open login button uses a clean URL.

## 0.3.0

- Add one-click Codex and Claude login flows that start the vendor CLI inside the add-on and show the browser login URL/code in the Web UI.
- Bundle the Codex and Claude Code CLIs in the add-on image so OAuth files can be generated inside persistent add-on storage.

## 0.2.2

- Add browser-based OAuth file upload for Codex and Claude credentials so setup no longer requires manual add-on file copying.
- Add provider-specific test endpoint and clearer OpenRouter setup links/errors.

## 0.2.1

- Fix OAuth credential discovery inside the add-on by setting `HOME=/config`, so CodexBar can read `/config/.codex/auth.json` and `/config/.claude/.credentials.json`.
- Sync the Home Assistant provider schema with the setup wizard provider list.

## 0.2.0

- Add an Ingress setup wizard for enabling providers and saving CodexBar config from the browser.
- Add an internal config API with validation and backups before overwriting config.
- Replace the static nginx-only dashboard with a Python setup UI that proxies CodexBar usage/cost endpoints.

## 0.1.0

- Initial Home Assistant add-on wrapper for CodexBar CLI serve.
- Add ingress nginx proxy and lightweight dashboard.
