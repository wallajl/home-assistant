# OpenClaw Gateway

Runs a full OpenClaw Gateway as a Home Assistant add-on and exposes a browser terminal through Home Assistant Ingress.

This is for installs where Home Assistant is the always-on machine. The add-on creates persistent OpenClaw state under `/data/openclaw`, stores the workspace in `workspace_dir`, starts `openclaw gateway run`, and opens a terminal panel where the user can configure OpenClaw.

## First Setup

Open the OpenClaw sidebar panel after installing the add-on, then use the terminal to configure providers and channels.

Useful commands:

- `openclaw configure`
- `openclaw channels add --channel telegram`
- `openclaw models auth login`
- `openclaw tui --local`

## Options

- `gateway_port`: Port for the OpenClaw Gateway.
- `gateway_bind`: Gateway bind mode. The default `loopback` is enough for the built-in terminal. Use `lan` only when other devices or add-on nodes need to connect.
- `gateway_auth`: Gateway auth mode, one of `none`, `token`, or `password`.
- `gateway_token`: Token used when `gateway_auth` is `token`.
- `gateway_password`: Password used when `gateway_auth` is `password`.
- `workspace_dir`: Persistent workspace path, usually under `/share`.

The terminal is protected by Home Assistant Ingress. If you expose the gateway itself with `gateway_bind: lan`, set `gateway_auth` to `token` or `password`.
