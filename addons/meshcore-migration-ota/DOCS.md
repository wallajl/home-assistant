# MeshCore Migration OTA: operation guide

## Purpose

This add-on performs one narrow migration:

1. Export the existing Meshtastic configuration and security keys over TCP.
2. Validate a non-merged MeshCore ESP32-S3 application binary.
3. Ask the reviewed Meshtastic build `2.6.11.60ec05e` to reboot into its failsafe Bluetooth OTA helper.
4. Upload the application through the Home Assistant host's local Bluetooth adapter.
5. Verify that MeshCore becomes reachable on TCP port `5000`.

It does **not** build MeshCore firmware, change Omada, or configure Wi-Fi after flashing. Wi-Fi and Adelaide MeshCore radio settings must already be present in the uploaded build.

This is an application-only cross-flash. It preserves the Meshtastic bootloader, partition table, NVS and data partitions; it is not equivalent to a clean MeshCore merged-image installation. The static review found the current application slot at `0x10000`, the failsafe helper at `0x340000`, and a `0x330000` slot capacity, but the path has not yet been proven on this physical board. Plan a later USB full installation/repartition if MeshCore's supported upgrade path requires it.

## Before installing

- Confirm the target is the 8 MB ESP32-S3 Heltec V3 running the reviewed Meshtastic build `2.6.11.60ec05e`. Do not use this protocol implementation against a different firmware without a new source review.
- Place the Home Assistant Bluetooth adapter within reliable BLE range.
- Keep USB recovery hardware physically available.
- Build the `Heltec_v3_companion_radio_wifi` **application** image privately. Do not use a merged image.
- Calculate the exact file's SHA-256 and set `expected_firmware_sha256` in the add-on configuration. The uploader rejects every other artifact.
- Confirm its size is no greater than `0x330000` bytes.
- Do not store the WLAN password or the resulting credential-bearing firmware in Git.

## Install

1. Refresh the existing `https://github.com/wallajl/home-assistant` add-on repository.
2. Install **MeshCore Migration OTA**.
3. Leave automatic start disabled.
4. Confirm the configured hosts. For this installation the add-on reaches the Home Assistant Meshtastic proxy through the Supervisor bridge at `172.30.32.1:4403`; the post-flash MeshCore target remains `192.168.0.181:5000`.
5. Start the add-on and open its Web UI.
6. Keep the Home Assistant Meshtastic integration loaded while creating the private backup, because it owns the working local TCP proxy.
7. Only after the backup succeeds, disable the Meshtastic integration before reboot-to-OTA and BLE discovery.

## Migrate

1. With the Meshtastic integration still loaded, click **Create backup**, wait for `backup ready`, then click **Download private backup**. Store the downloaded YAML somewhere private and confirm it is non-empty.
2. Select the non-merged MeshCore `.bin` file and click **Validate and stage**.
3. Review the detected project, byte size and SHA-256 digest.
4. Disable the Meshtastic integration before preparing/rebooting the node into BLE OTA mode.
5. Confirm that USB recovery is available and open the add-on log to obtain the one-use arming code.
6. Click **Reboot to OTA and scan**. This enters the failsafe helper but does not write firmware.
7. Review the displayed OTA name, exact BLE address, RSSI and staged firmware hash. If needed, move the adapter closer and use **Rescan without reboot**.
8. Re-enter the exact scanned BLE address, the displayed confirmation phrase and the one-use arming code.
9. Click **Flash exact device once**.
10. Do not stop the add-on, reboot Home Assistant, power-cycle the Heltec, or retry while the phase is `connected`, `flashing` or `verifying`.
11. Wait for `complete`, which means TCP port `5000` responded. A transport-complete message without TCP verification is not treated as a successful boot.

## Failure handling

- **Fails before `flashing`:** do not repeatedly power-cycle. Check whether Meshtastic returns on TCP `4403`; otherwise use USB recovery.
- **Fails during `flashing`:** treat the main application slot as incomplete. Do not assume the transfer is resumable. Recover through USB.
- **Transfer completes but verification fails:** check Omada for the Heltec's DHCP lease and verify the compiled Wi-Fi settings. If it never joins the WLAN, use USB recovery.
- **MeshCore appears:** verify receive/send behavior and the Home Assistant MeshCore integration before removing the old Meshtastic integration.

## Remove

After successful migration and verification:

1. Stop and uninstall this add-on.
2. Delete any staged credential-bearing firmware from the add-on data during uninstall.
3. Confirm the downloaded Meshtastic backup remains in its intended private backup location before uninstalling.
