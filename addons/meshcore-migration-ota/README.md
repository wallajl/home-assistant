# MeshCore Migration OTA

A temporary, experimental Home Assistant add-on for moving an ESP32-S3 Heltec V3 running Meshtastic `2.6.11.60ec05e` to a **non-merged MeshCore Wi-Fi Companion application** through Meshtastic's legacy failsafe Bluetooth OTA receiver.

## Safety boundary

This is an unsupported cross-firmware migration. Use the normal USB full-flash process whenever possible.

- Keep a USB data cable and a computer ready for recovery.
- The Home Assistant machine's local Bluetooth adapter must be within range of the Heltec V3.
- Back up through the working Meshtastic TCP endpoint first. On this Home Assistant installation that endpoint is the local proxy at `192.168.0.254:4403`, which exists only while the Meshtastic integration is loaded.
- After the private backup succeeds, temporarily disable the Home Assistant Meshtastic integration before rebooting the radio into BLE OTA mode.
- Never upload a merged/full-flash image. The add-on rejects bootloader and merged images.
- A BLE disconnect after flash writing begins can leave the application slot incomplete and require USB recovery.
- Do not power-cycle the radio during transfer.
- The uploaded MeshCore build must already contain the intended WLAN configuration and MeshCore radio settings.
- Firmware is staged privately in the add-on's `/data` volume and is never committed to this repository.
- Meshtastic backups are retained with mode `0600` in private add-on storage and must be downloaded through authenticated ingress before flashing.

The add-on is admin-only through Home Assistant ingress, starts manually, never starts at boot, and uses an enforced AppArmor policy. The ingress web process has no host D-Bus permission; only an argument-free compiled launcher can transition into a fixed-purpose worker profile restricted to `org.bluez` and `org.freedesktop.DBus`. Destructive requests also require the authenticated ingress user, a CSRF nonce, an exact scanned BLE address, and a one-use arming code printed only in the add-on log. Uninstall the add-on after the migration.

See [DOCS.md](DOCS.md) for installation and operation.
