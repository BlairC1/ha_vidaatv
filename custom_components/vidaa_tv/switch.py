"""Switch platform for Hisense TV.

Provides a "Debug logging" toggle that turns on verbose logging for this
integration and for pyvidaa, without editing configuration.yaml or restarting.

It does exactly what this YAML would do::

    logger:
      logs:
        custom_components.vidaa_tv: debug
        pyvidaa: debug

by setting those logger levels directly - which is what Home Assistant's own
``logger`` integration does internally. Turning it off restores the previous
levels, so it inherits whatever the rest of your logging config specifies.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

# Both the integration's own logger and the underlying library. pyvidaa logs the
# MQTT/auth layer, which is where connection and credential problems show up.
DEBUG_LOGGERS = ("custom_components.vidaa_tv", "pyvidaa")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hisense TV switches."""
    async_add_entities(
        [VidaaTVDebugLoggingSwitch(entry.runtime_data.coordinator, entry)]
    )


class VidaaTVDebugLoggingSwitch(VidaaTVEntity, SwitchEntity, RestoreEntity):
    """Turns verbose logging on for the integration and pyvidaa."""

    _attr_name = "Debug logging"
    _attr_translation_key = "debug_logging"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bug-outline"

    def __init__(self, coordinator, entry) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, entry)
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_debug_logging"
        self._attr_is_on = False
        # Remember the levels so turning the switch off restores whatever the
        # user's own logging configuration had set, rather than forcing a value.
        self._previous_levels: dict[str, int] = {}

    async def async_added_to_hass(self) -> None:
        """Re-apply the setting after a restart.

        Debug logging is most useful for problems that happen during startup, so
        losing it on every reboot defeated the point. RestoreEntity gives us the
        previous state without writing to the config entry - which would trigger
        the update listener and reload the integration on every toggle.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state == STATE_ON:
            self._apply(True)
            self._attr_is_on = True
            _LOGGER.debug("Debug logging restored to on after restart")

    async def async_will_remove_from_hass(self) -> None:
        """Restore logger levels if the entity goes away."""
        if self._attr_is_on:
            self._apply(False)
        await super().async_will_remove_from_hass()

    @property
    def available(self) -> bool:
        """Always available: a local setting, not a TV query."""
        return True

    @property
    def is_on(self) -> bool:
        """Return whether verbose logging is enabled."""
        return self._attr_is_on

    def _apply(self, enable: bool) -> None:
        """Set or restore the logger levels."""
        for name in DEBUG_LOGGERS:
            logger = logging.getLogger(name)
            if enable:
                self._previous_levels.setdefault(name, logger.level)
                logger.setLevel(logging.DEBUG)
            else:
                logger.setLevel(self._previous_levels.pop(name, logging.NOTSET))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable verbose logging."""
        self._apply(True)
        self._attr_is_on = True
        self.async_write_ha_state()
        _LOGGER.debug("Debug logging enabled via switch (integration + pyvidaa)")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Restore the previous logging levels."""
        _LOGGER.debug("Debug logging disabled via switch")
        self._apply(False)
        self._attr_is_on = False
        self.async_write_ha_state()
