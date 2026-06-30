# Telegram Chat GUI

Telegram Chat GUI is a Home Assistant add-on that puts Telegram Web in the Home Assistant sidebar.

It is intentionally an add-on, not an integration. It does not create entities, services, automations, or connect to Home Assistant's API. Home Assistant only provides the sidebar entry and Ingress proxy.

## How it works

- Starts a Chromium browser inside the add-on container.
- Opens Telegram Web automatically.
- Shows the browser through noVNC in a Home Assistant sidebar panel.
- Stores the Chromium profile in the add-on's persistent `/data` folder.

Log into Telegram Web once from the sidebar. The session should remain available after add-on restarts unless Telegram signs it out or the add-on data is removed.

## Install

1. Add this repository to the Home Assistant add-on store:
   `https://github.com/wallajl/home-assistant`
2. Install **Telegram Chat GUI**.
3. Start the add-on.
4. Open **Telegram** from the Home Assistant sidebar.
5. Log into Telegram Web.

## Notes

This add-on gives anyone with access to the Home Assistant sidebar access to the logged-in Telegram Web session. Only install it on a Home Assistant instance and user account you trust.
