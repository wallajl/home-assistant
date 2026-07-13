# CodexBar add-on documentation

## What it does

This focused Home Assistant add-on shows **OpenAI Codex** and **Claude** subscription usage together. It uses CodexBar as the usage engine and exposes only a Home Assistant Ingress panel—no host port or desktop browser is required inside the container.

The dashboard includes one combined seven-day graph of each provider's weekly quota used. A background worker samples both providers every five minutes, even when the panel is closed, and retains only seven days of real observations.

## Setup

1. Install and start the add-on.
2. Enable **Show in sidebar** on the add-on page if Home Assistant has not added it automatically.
3. Open **CodexBar** from the Home Assistant sidebar.
4. Open **Settings**, then click **Log in to Codex**:
   - the add-on runs `codex login --device-auth` inside its container;
   - the panel displays `https://auth.openai.com/codex/device` and a one-time code;
   - open the link and enter the displayed code.
5. From **Settings**, click **Log in to Claude**:
   - the add-on runs `claude auth login --claudeai` inside its container;
   - the panel displays the Claude authorization URL;
   - complete login and, if Claude returns a code, paste it into the panel.
6. Return to **Dashboard** and press **Refresh usage**.

The official CLIs save credentials directly into persistent add-on storage:

- Codex: `/config/.codex/auth.json`
- Claude: `/config/.claude/.credentials.json`
- CodexBar config: `/config/codexbar/config.json`

No credential file upload or manual JSON setup is required.

## Background collection and authentication

- Collection starts automatically with the add-on and does not depend on an open browser tab.
- One sample per provider is recorded every five minutes in `/config/codexbar/history.json`.
- A bounded, secret-free activity log is kept in `/config/codexbar/activity.log`.
- Before each sample, `claude auth status --json` lets the official Claude CLI refresh its own OAuth credentials without sending a prompt or consuming model usage.
- No fake or interpolated historical data is generated. A new installation starts with its first real sample and fills the graph over the following seven days.

## Why login is shown as a URL/code

Home Assistant add-ons run in containers and cannot launch a desktop browser on your phone or computer. The panel therefore captures the official CLI URL/device code and displays it in Home Assistant. Codex uses a device code. Claude uses a browser URL and may ask you to paste an authorization code back into the panel.

## Options

- `codexbar_version`: pinned upstream CodexBar CLI release.
- `refresh_interval`: CodexBar response cache TTL.
- `history_interval`: background trend sampling interval; defaults to 300 seconds.
- `request_timeout`: provider request timeout.
- `log_level`: CodexBar logging level.

## Troubleshooting

Open **Login details** or **Diagnostics** in the panel. Request failures include the endpoint and HTTP status. The add-on logs also show whether the Python Ingress UI, CodexBar backend, or provider CLI failed.

## Security

The add-on is available only through authenticated Home Assistant Ingress and does not publish a host port. OAuth tokens remain under the add-on's persistent `/config` storage. Treat displayed one-time codes and authorization URLs as temporary secrets.

Trend history contains only timestamps, provider names, and weekly percentage used—never account identifiers or credentials.
