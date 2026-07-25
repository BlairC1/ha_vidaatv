"""Shared entity base for the Hisense TV integration.

Every platform needs the same DeviceInfo, so it lives here once rather than
being copy-pasted per platform (which is how media_player and remote drifted
apart in the past).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_NAME,
    CONF_SW_VERSION,
    DEFAULT_NAME,
    DOMAIN,
)
from .coordinator import VidaaTVDataUpdateCoordinator

if TYPE_CHECKING:
    pass


def format_mac(value: str | None) -> str | None:
    """Format a bare 12-hex string as a colon-separated MAC."""
    if not value:
        return None
    raw = value.replace(":", "").replace("-", "").lower()
    if len(raw) != 12 or not all(c in "0123456789abcdef" for c in raw):
        return None
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2)).upper()


class VidaaTVEntity(CoordinatorEntity[VidaaTVDataUpdateCoordinator]):
    """Base entity binding all platforms to the one TV device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VidaaTVDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = entry.data.get(CONF_DEVICE_ID)

    @property
    def available(self) -> bool:
        """Return whether the coordinator last updated successfully."""
        return self.coordinator.last_update_success and self.coordinator.available

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info, preferring the TV's live values."""
        data = self.coordinator.device_data
        # Stable identity: keep the existing identifier (device_id or entry_id);
        # do not switch to a MAC for existing installs (that would orphan the
        # device and all its entities).
        device_id = self._entry.data.get(CONF_DEVICE_ID) or self._entry.entry_id
        mac = format_mac(
            data.get("device_id") or self._entry.data.get(CONF_DEVICE_ID)
        )

        info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=data.get("name") or self._entry.data.get(CONF_NAME, DEFAULT_NAME),
            manufacturer="Hisense",
            model=data.get("model") or self._entry.data.get(CONF_MODEL),
            sw_version=data.get("sw_version")
            or self._entry.data.get(CONF_SW_VERSION),
        )
        if mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        if data.get("ip"):
            info["configuration_url"] = f"http://{data['ip']}"
        return info
