"""Button platform for Hisense TV.

Just one button: Audio only.

It is a button rather than a switch deliberately. ``ONLY_AUDIO`` toggles the
panel, but the TV does not expose the panel's state anywhere - verified by
diffing every field of all five queryable actions (gettvinfo, getdeviceinfo,
capability, sourcelist, state) with the picture on and off, which showed no
difference at all, and by watching the broadcast topics while toggling, which
produced nothing. A switch would therefore have to guess, and would silently
desync the moment anyone used the physical remote. A button claims no state and
is honest about what it does.

The key is ``KEY_ONLY_AUDIO`` - conventional ``KEY_`` prefix, but note the word
order (ONLY_AUDIO, not AUDIO_ONLY). It appears in none of the published VIDAA
key lists, which is why it had to be found by sweeping candidates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 1

# Toggles the panel off/on while audio keeps playing.
AUDIO_ONLY_KEY = "KEY_ONLY_AUDIO"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hisense TV buttons."""
    async_add_entities([VidaaTVAudioOnlyButton(entry.runtime_data.coordinator, entry)])


class VidaaTVAudioOnlyButton(VidaaTVEntity, ButtonEntity):
    """Toggles audio-only mode (screen off, sound on)."""

    _attr_name = "Audio only"
    _attr_translation_key = "audio_only"
    _attr_icon = "mdi:television-off"

    def __init__(self, coordinator, entry) -> None:
        """Initialise the button."""
        super().__init__(coordinator, entry)
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_audio_only"

    async def async_press(self) -> None:
        """Toggle the panel."""
        await self.coordinator.async_send_key(AUDIO_ONLY_KEY)
