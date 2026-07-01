# Hermes Agent

Runs the official NousResearch Hermes Agent Docker image as a Home Assistant add-on and exposes a browser terminal through Home Assistant Ingress.

Hermes state is persisted in the add-on data directory under `/data/hermes`, then linked into the official image path at `/opt/data`.

## First Setup

Open the Hermes sidebar panel after installing the add-on. From the terminal, run one of:

- `hermes setup --portal`
- `hermes setup`
- `hermes model`
- `hermes setup gateway`
- `hermes --tui`

The add-on can optionally start `hermes gateway run` in the background while the terminal remains available.

## Options

- `start_gateway`: Start the Hermes gateway automatically.
- `gateway_allow_all_users`: Set `GATEWAY_ALLOW_ALL_USERS`. Keep this off unless you understand the exposure.
- `hass_url`: Home Assistant URL visible from inside the add-on.
- `hass_token`: Optional Home Assistant long-lived access token.
- `telegram_bot_token`: Optional Telegram bot token.
- `telegram_allowed_users`: Optional comma-separated Telegram user allowlist.

For a simple first run, leave secrets blank, open the terminal, and use Hermes' own setup flow.
