# CodexBar add-on documentation

## What it does

- Downloads the selected CodexBar CLI release for your Home Assistant architecture.
- Runs `codexbar serve` on `127.0.0.1:8080` for usage/cost JSON.
- Serves a Home Assistant Ingress setup wizard and dashboard on port `8099`.
- Lets you enable providers, paste API keys/cookies/base URLs, save config, validate it, and test providers from the browser.
- Persists CodexBar config under the add-on configuration mount.

## First setup

1. Install and start the add-on.
2. Open the add-on Web UI.
3. In **Setup wizard**, enable providers such as Codex, Claude, OpenAI, OpenRouter, LiteLLM, or LLM Proxy.
4. Paste only the credentials needed for that provider.
5. Press **Save setup**.
6. Press **Test** on a provider or open the **Dashboard** tab.

The setup UI writes to:

- `CODEXBAR_CONFIG=/config/codexbar/config.json`
- `HOME=/config/home`

Before overwriting an existing config, the setup API creates a timestamped backup beside it.

## Local CLI credentials

Codex and Claude OAuth are not configured with normal API keys. CodexBar reads the OAuth files created by the normal CLI login flows.

On a computer where you are already logged in:

- Codex OAuth file: `~/.codex/auth.json`
- Claude OAuth file: `~/.claude/.credentials.json`

Copy them into the add-on configuration storage so they appear inside the add-on container as:

- Codex files: `/config/.codex`
- Claude files: `/config/.claude`

Then restart the add-on and use the dashboard provider source `auto` or `oauth`.

If you want organization-level Claude Admin API spend instead of your Claude OAuth account limits, use an Anthropic Admin API key (`sk-ant-admin...`) in the Claude provider card. Codex does not have a useful API-key setup for ChatGPT/Codex subscription quota; use OAuth via `~/.codex/auth.json`.

## Advanced configuration

The add-on still supports the original add-on options:

- `codexbar_version`: upstream CodexBar CLI release version to install.
- `default_provider`: dashboard default provider.
- `refresh_interval`: CodexBar response cache TTL.
- `request_timeout`: per-request timeout.
- `log_level`: CodexBar CLI verbosity.
- `provider_config_json`: optional bootstrap JSON for first start or forced overwrite when `auto_seed_config` is enabled.

For day-to-day setup, prefer the Web UI over editing `provider_config_json`.

## Security

The CodexBar API has no built-in authentication. This add-on intentionally exposes it through Home Assistant Ingress and does not publish a host port. Treat provider API keys, OAuth tokens, and cookie headers as secrets.
