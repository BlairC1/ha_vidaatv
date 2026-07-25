"""Binary sensor platform for Hisense TV.

Provides an "In use" signal that is stricter than the media player's on/off.

Why this exists: ``gettvinfo``'s ``fake_sleep_state`` tells us whether the TV is
awake, but the TV also wakes itself in the small hours to check for updates -
awake, panel off, nobody watching. For automations ("is someone actually
watching?") that nightly wake is a false positive. The activity ``statetype``
distinguishes the two: a TV genuinely in use reports an interactive state
(sourceswitch / livetv / app / launcher / settings / EPG), whereas the
maintenance wake sits on ``fake_sleep_1``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 0

# Activity states that mean a person is (or could be) watching. Anything
# fake_sleep_* is either standby or a panel-off wake.
ACTIVE_STATETYPES = frozenset(
    {
        "sourceswitch",
        "livetv",
        "app",
        "remote_launcher",
        "remote_setting",
        "remote_epg",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hisense TV binary sensors."""
    async_add_entities([VidaaTVInUseSensor(entry.runtime_data.coordinator, entry)])


class VidaaTVInUseSensor(VidaaTVEntity, BinarySensorEntity):
    """True only when the TV is awake AND showing something."""

    _attr_name = "In use"
    _attr_translation_key = "in_use"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:television-play"

    def __init__(self, coordinator, entry) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, entry)
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_in_use"

    @property
    def is_on(self) -> bool:
        """Return True when the TV is awake and in an interactive state."""
        data = self.coordinator.data or {}
        if not data.get("is_on"):
            return False
        return data.get("statetype") in ACTIVE_STATETYPES

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the underlying signals so the distinction is inspectable."""
        data = self.coordinator.data or {}
        return {
            "statetype": data.get("statetype"),
            "source": data.get("source"),
        }
