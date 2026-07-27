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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 0

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


class VidaaTVDebugLoggingSwitch(VidaaTVEntity, SwitchEntity):
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
        logging.getLogger(DEBUG_LOGGERS[0]).debug(
            "Debug logging enabled via switch (integration + pyvidaa)"
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Restore the previous logging levels."""
        logging.getLogger(DEBUG_LOGGERS[0]).debug("Debug logging disabled via switch")
        self._apply(False)
        self._attr_is_on = False
        self.async_write_ha_state()
