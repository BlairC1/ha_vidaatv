"""Binary sensor platform for Hisense TV.

Provides an "In use" signal that is stricter than the media player's on/off.

Driven by ``is_on``, which comes from the live ``gettvinfo`` query
(``fake_sleep_state``). That query already excludes the nightly maintenance
wake - measured during one: the cached broadcast said ``fake_sleep_1`` while
``fake_sleep_state`` correctly reported 0.

Do NOT gate this on ``statetype``. That value is the last *cached broadcast*,
not a live reading: after a connect it sits on ``fake_sleep_1`` until the TV
happens to announce a change, so during ordinary viewing it reports
``fake_sleep_1`` and the sensor read "off" while the TV was plainly in use.
``statetype`` is still exposed as an attribute for context.
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
        """Return True when the TV is genuinely awake and in use."""
        return bool((self.coordinator.data or {}).get("is_on"))

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Expose the underlying signals so the distinction is inspectable."""
        data = self.coordinator.data or {}
        return {
            # Cached broadcast value - context only, see the class docstring.
            "statetype": data.get("statetype"),
            "source": data.get("source"),
            "source_detail": data.get("source_detail"),
        }
