"""Button platform for Hisense TV.

Exposes the handful of remote keys that are genuinely useful as one-tap
dashboard buttons or automation targets. The full key set stays available via
the ``vidaa_tv.send_key`` service and the remote entity; duplicating all 60+
keys as entities would just clutter the registry.

Only Home, Back and Menu are enabled by default - the rest are registered but
disabled so they can be turned on individually without cluttering new installs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class VidaaButtonDescription(ButtonEntityDescription):
    """Describes a Hisense TV key button."""

    key_code: str


BUTTONS: tuple[VidaaButtonDescription, ...] = (
    VidaaButtonDescription(
        key="home", name="Home", icon="mdi:home", key_code="KEY_HOME"
    ),
    VidaaButtonDescription(
        key="back", name="Back", icon="mdi:arrow-left", key_code="KEY_BACK"
    ),
    VidaaButtonDescription(
        key="menu", name="Menu", icon="mdi:menu", key_code="KEY_MENU"
    ),
    VidaaButtonDescription(
        key="exit",
        name="Exit",
        icon="mdi:exit-to-app",
        key_code="KEY_EXIT",
        entity_registry_enabled_default=False,
    ),
    VidaaButtonDescription(
        key="info",
        name="Info",
        icon="mdi:information-outline",
        key_code="KEY_INFO",
        entity_registry_enabled_default=False,
    ),
    VidaaButtonDescription(
        key="subtitle",
        name="Subtitles",
        icon="mdi:subtitles-outline",
        key_code="KEY_SUBTITLE",
        entity_registry_enabled_default=False,
    ),
    VidaaButtonDescription(
        key="channel_up",
        name="Channel up",
        icon="mdi:arrow-up-bold",
        key_code="KEY_CHANNELUP",
        entity_registry_enabled_default=False,
    ),
    VidaaButtonDescription(
        key="channel_down",
        name="Channel down",
        icon="mdi:arrow-down-bold",
        key_code="KEY_CHANNELDOWN",
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Hisense TV buttons."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        VidaaTVButton(coordinator, entry, description) for description in BUTTONS
    )


class VidaaTVButton(VidaaTVEntity, ButtonEntity):
    """Sends a single remote key."""

    entity_description: VidaaButtonDescription

    def __init__(self, coordinator, entry, description: VidaaButtonDescription) -> None:
        """Initialise the button."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_button_{description.key}"

    async def async_press(self) -> None:
        """Send the key to the TV."""
        await self.coordinator.async_send_key(self.entity_description.key_code)
