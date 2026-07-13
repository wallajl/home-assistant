# Changelog

## 0.4.1

- Open on a clean usage dashboard by default.
- Move Codex/Claude login status, reconnect controls, login flow, and diagnostics into a separate Settings page.

## 0.4.0

- Reset the add-on to a focused Codex + Claude experience.
- Remove API-key providers, manual OAuth upload, raw provider configuration, and unrelated setup controls.
- Add container-safe Codex device-code and Claude URL/code login flows with visible URL, code, CLI output, and error diagnostics.
- Handle Home Assistant Ingress base paths and chunked POST bodies.
- Show Codex and Claude usage/cost together in one sidebar panel.

## 0.3.3

- Decode chunked request bodies from the Home Assistant ingress proxy so Save/Login/Cancel POSTs no longer fail with "Invalid JSON" from the real HA menu.
- Inject the ingress base path (`X-Ingress-Path`) into the UI as a `<base>` tag so API calls resolve with or without a trailing slash in the browser URL.
- Strip OSC terminal hyperlink escapes from login output so the Claude OAuth URL is extracted cleanly instead of doubled/corrupted.
- Add a code-paste box wired to the login CLI's stdin so `claude auth login` ("Paste code here if prompted >") can actually complete.
- Show the Codex one-time device code prominently and detect when the CLI is waiting for input.
- Create `/config/.codex` and `/config/.claude` before starting logins (Codex CLI exits immediately if its home directory is missing).
- Report every failed UI request with its endpoint, HTTP status, and message; the watchdog `/health` endpoint now reflects setup-UI health with backend state in the payload instead of restarting the add-on while the backend warms up.

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
