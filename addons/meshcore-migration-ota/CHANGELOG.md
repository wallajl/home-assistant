# Changelog

## 0.1.2

- Restore validated staged firmware and private backups after an add-on update or restart.
- Permit a non-writing BLE rescan when the radio is already in failsafe OTA mode.
- Clarify that Prepare owns the reboot-to-OTA command and needs the Meshtastic proxy loaded.

## 0.1.1

- Use the Supervisor bridge address for the Home Assistant-hosted Meshtastic backup proxy.

## 0.1.0

- Initial experimental one-use migration add-on.
- Validate non-merged MeshCore ESP32-S3 application images.
- Export Meshtastic configuration and keys before enabling migration.
- Upload through the Meshtastic failsafe BLE OTA protocol with per-chunk acknowledgements.
- Verify MeshCore TCP availability after reboot.
