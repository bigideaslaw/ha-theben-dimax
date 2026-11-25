# Theben DimMax 540 for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/bigideaslaw/ha-theben-dimmax/actions/workflows/validate.yml/badge.svg)](https://github.com/bigideaslaw/ha-theben-dimmax/actions/workflows/validate.yml)

Local Bluetooth Mesh control of **Theben DimMax 540** dimmers from Home Assistant. There's no cloud and no Theben gateway. Home Assistant talks to the mesh directly through its Bluetooth adapter or an ESPHome Bluetooth proxy.

> [!WARNING]
> **Beta.** This integration has been tested on **one DimMax 540** so far. It may not work with other firmware versions or hardware revisions. If you try it, please [send a device report](https://github.com/bigideaslaw/ha-theben-dimmax/issues/new?template=device_report.yml), whether it works or not.

## Features

- **Light** entity with on/off and brightness, one per channel on multi-channel dimmers
- **Power, voltage, current and temperature** sensors, plus a Min Dim Level sensor. Power is calculated from the dimmer's measured line voltage and output current, the same way the Theben app's Energy Monitor does it.
- One shared mesh connection serves every DimMax in the same network, so adding more dimmers doesn't add more BLE connections
- The mesh sequence number is persisted across restarts so the dimmers don't reject commands as replays

## Requirements

- Home Assistant 2024.8 or newer with a working [Bluetooth](https://www.home-assistant.io/integrations/bluetooth/) adapter or ESPHome Bluetooth proxy in range of a dimmer
- The dimmer already set up (provisioned) in the official Theben app
- Your mesh **NetKey** and **AppKey**. The easiest way is the Theben app's project export file.

## Installation

### HACS (recommended)

1. In HACS, open the menu (⋮) → **Custom repositories**
2. Add `https://github.com/bigideaslaw/ha-theben-dimmax` with category **Integration**
3. Search for **Theben DimMax**, install it, and restart Home Assistant

### Manual

Copy `custom_components/theben_dimmax` into your Home Assistant `config/custom_components/` folder and restart.

## Setup

Go to **Settings → Devices & services → Add integration → Theben DimMax** and pick one of:

- **Import from Theben app**: paste the full JSON from your Theben app project export. If it contains several dimmers, you'll be asked which one to add. Repeat for each dimmer.
- **Manual**: enter the BLE MAC address, NetKey, AppKey, provisioner address, dimmer unicast address, IV index and a starting sequence number.

> **Status updates:** state is polled every 10 seconds. If you switch a dimmer at the wall, it shows up in HA within that interval. For instant updates, configure the dimmer's OnOff and Level models in the Theben app to publish status to the provisioner.

## Tested devices

| Device | Firmware | Integration version | Result | Reported by |
| --- | --- | --- | --- | --- |
| DimMax 540 | (add your firmware version) | v2.1.0 | Works | @bigideaslaw |

Tried it on your dimmer? [Add your result](https://github.com/bigideaslaw/ha-theben-dimmax/issues/new?template=device_report.yml).

## Troubleshooting

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.theben_dimmax: debug
```

Please include debug logs when opening an [issue](https://github.com/bigideaslaw/ha-theben-dimmax/issues). **Remove your NetKey and AppKey** from anything you post.

## Disclaimer

This is an independent community project. It is not affiliated with, endorsed by or supported by Theben AG. "Theben" and "DimMax" are trademarks of their respective owner. Use at your own risk.

## Credits

Licensed under the [MIT License](LICENSE).
