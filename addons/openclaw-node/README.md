# OpenClaw Node

Runs the OpenClaw headless node host as a Home Assistant add-on.

The add-on starts `openclaw node run` inside the add-on container and connects it to an existing OpenClaw Gateway. It stores OpenClaw node state under the add-on data directory so pairing survives restarts.

This add-on does not ship with a preconfigured gateway. Set `gateway_host` and `gateway_port` after installing it. The gateway must listen on an address that Home Assistant can reach; a loopback-only gateway at `127.0.0.1` on another machine will not work.

## Options

- `gateway_host`: OpenClaw Gateway host or IP address.
- `gateway_port`: OpenClaw Gateway node port.
- `tls`: Enable TLS for the gateway connection.
- `tls_fingerprint`: Expected TLS certificate fingerprint when TLS is enabled.
- `display_name`: Name shown for this node in OpenClaw.
- `node_id`: Optional fixed node id. Leave blank unless you intentionally want to override the generated node identity.

The add-on uses host networking so it can reach a LAN gateway without Docker bridge surprises. It maps common Home Assistant folders into the container so the node can inspect or work with Home Assistant files when OpenClaw grants that capability.
