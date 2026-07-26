"""Switch platform for Hisense TV.

Provides a "Debug logging" toggle. Home Assistant hides DEBUG-level messages
unless the logger is configured in configuration.yaml (which needs a restart),
so this switch instead promotes the integration's verbose lines from DEBUG to
INFO. Flip it on, reproduce the problem, read the log, flip it off - no YAML
editing and no restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hisense TV switches."""
    async_add_entities([VidaaTVDebugLoggingSwitch(entry.runtime_data.coordinator, entry)])


class VidaaTVDebugLoggingSwitch(VidaaTVEntity, SwitchEntity):
    """Promotes this integration's verbose logging to INFO while on."""

    _attr_name = "Debug logging"
    _attr_translation_key = "debug_logging"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bug-outline"

    def __init__(self, coordinator, entry) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, entry)
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_debug_logging"

    @property
    def available(self) -> bool:
        """Always available: it is a local setting, not a TV query."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether verbose logging is enabled."""
        return bool(getattr(self.coordinator, "debug_logging", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start logging verbose lines at INFO."""
        self.coordinator.debug_logging = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Return verbose lines to DEBUG."""
        self.coordinator.debug_logging = False
        self.async_write_ha_state()
