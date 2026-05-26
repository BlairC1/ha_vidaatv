# Changelog

All notable changes to the Vidaa TV Home Assistant integration will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

(Library/protocol changes are tracked separately in the [`pyvidaa`](https://github.com/warrenrees/pyvidaa) repository.)

## [2.0.1]

### Fixed

- The device now shows the TV's model, firmware version, IP, and MAC. The coordinator caches
  `getdeviceinfo` and the entities build their `DeviceInfo` from it; previously the info was
  fetched during the first refresh (before the device existed in the registry) and never
  applied, so model/firmware stayed blank.
- Pairing no longer re-prompts for a PIN when the TV is briefly slow to return device info
  after a successful authentication. `getdeviceinfo` is retried, and a miss is treated as
  non-fatal — the entry is created and device info is fetched after setup.

### Changed

- Diagnostics now include the coordinator's cached `device_data` (model, firmware, IP) so the
  device info the integration resolved is visible.

## [2.0.0]

Initial release of the Vidaa TV integration as a standalone repository, split out of the
`pyvidaa` project. The integration uses the `pyvidaa` library (from PyPI) for all TV
communication.

### Added

- Home Assistant integration for Hisense/Vidaa Smart TVs — domain `vidaa_tv`, display name
  "Vidaa TV": media player, remote, config flow (SSDP discovery + PIN pairing), diagnostics,
  and repair flows.
- VIDAA brand images (icon/logo) via the local `brand/` folder (HA 2026.3+).
- Remote: shows "Home" as the current activity when the TV is at the launcher.
