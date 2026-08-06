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

import asyncio
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

# Toggles audio-only mode (panel off, sound on). Not in any published key list.
AUDIO_ONLY_KEY = "KEY_AUDIO"
# Any key press wakes the panel. KEY_INFO is used because it is harmless: it
# shows a brief overlay that dismisses itself, unlike navigation keys which
# could select or exit something in an app.
WAKE_KEY = "KEY_INFO"
# Gap between the wake and the toggle, so the TV processes them in order.
KEY_GAP = 0.6

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
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            VidaaTVDebugLoggingSwitch(coordinator, entry),
            VidaaTVAudioOnlySwitch(coordinator, entry),
        ]
    )


class VidaaTVAudioOnlySwitch(VidaaTVEntity, SwitchEntity, RestoreEntity):
    """Audio-only mode: panel off, sound still playing.

    KEY_AUDIO only TOGGLES the panel, and the TV exposes its panel state
    nowhere - verified by diffing every field of all five queryable actions with
    the picture on and off (no difference) and by watching the broadcast topics
    while toggling (nothing). A switch driven by a bare toggle would therefore
    drift out of sync the moment its assumed state was wrong.

    The fix is to make both operations idempotent by exploiting the fact that
    ANY key press wakes the panel:

        turn on  -> wake key, then KEY_AUDIO   => panel off, whatever the start
        turn off -> wake key                   => panel on,  whatever the start

    Neither depends on knowing the current state, so a stale assumption
    self-corrects on the next command. The reported state is still optimistic -
    a press on the physical remote cannot be seen - but it can only be wrong
    until the next time the switch is used.
    """

    _attr_name = "Audio only"
    _attr_translation_key = "audio_only"
    _attr_icon = "mdi:television-off"

    def __init__(self, coordinator, entry) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, entry)
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_audio_only"
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        """Restore the last known position (optimistic, see class docstring)."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == STATE_ON

    @property
    def available(self) -> bool:
        """Only usable while the TV is on."""
        return bool((self.coordinator.data or {}).get("is_on"))

    @property
    def is_on(self) -> bool:
        """Return the assumed audio-only state."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Panel off, sound on."""
        _LOGGER.debug("Audio only on: %s then %s", WAKE_KEY, AUDIO_ONLY_KEY)
        await self.coordinator.async_send_key(WAKE_KEY)
        await asyncio.sleep(KEY_GAP)
        await self.coordinator.async_send_key(AUDIO_ONLY_KEY)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Panel back on - any key wakes it."""
        _LOGGER.debug("Audio only off: %s", WAKE_KEY)
        await self.coordinator.async_send_key(WAKE_KEY)
        self._attr_is_on = False
        self.async_write_ha_state()


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
