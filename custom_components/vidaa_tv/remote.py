"""Remote platform for Hisense TV."""

from __future__ import annotations

import asyncio
import voluptuous as vol

import logging
from typing import TYPE_CHECKING, Any, Iterable

from homeassistant.components.remote import RemoteEntity, RemoteEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ACTIVITY_HOME,
    ATTR_APP,
    ATTR_KEY,
    SERVICE_LAUNCH_APP,
    SERVICE_SEND_KEY,
    STATE_REMOTE_LAUNCHER,
)
from .coordinator import VidaaTVDataUpdateCoordinator
from .entity import VidaaTVEntity
from .helpers import build_source_list, resolve_source_id

# Import key utilities from the library
from pyvidaa.keys import get_key

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from . import VidaaTVConfigEntry

_LOGGER = logging.getLogger(__name__)


PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hisense TV remote from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([VidaaTVRemote(coordinator, entry)])

    # Registered as ENTITY services so Home Assistant resolves the target for
    # us. Previously these were plain domain services whose schema rejected
    # `entity_id`, so every targeted call failed validation before reaching the
    # integration - and the handler ignored the target anyway, firing at every
    # configured TV at once.
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SEND_KEY,
        {vol.Required(ATTR_KEY): cv.string},
        "async_send_key_service",
    )
    platform.async_register_entity_service(
        SERVICE_LAUNCH_APP,
        {vol.Required(ATTR_APP): cv.string},
        "async_launch_app_service",
    )


class VidaaTVRemote(VidaaTVEntity, RemoteEntity):
    """Representation of a Hisense TV remote."""

    _attr_has_entity_name = True
    _attr_name = "Remote"
    _attr_supported_features = RemoteEntityFeature.ACTIVITY

    def __init__(
        self,
        coordinator: VidaaTVDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the remote."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = (
            f"{self._device_id}_remote" if self._device_id else f"{entry.entry_id}_remote"
        )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available

    @property
    def is_on(self) -> bool | None:
        """Return if TV is on."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("is_on", False)

    @property
    def current_activity(self) -> str | None:
        """Return current activity (app name, source, or the home screen)."""
        data = self.coordinator.data
        if not data:
            return None
        activity = data.get("app") or data.get("source")
        if activity:
            return activity
        # At the launcher/home screen the TV reports neither an app nor a
        # source; surface "Home" so the remote shows a current activity.
        if data.get("is_on") and data.get("statetype") == STATE_REMOTE_LAUNCHER:
            return ACTIVITY_HOME
        return None

    @property
    def activity_list(self) -> list[str] | None:
        """Home + inputs + apps, derived from coordinator data (no local fetch)."""
        data = self.coordinator.data or {}
        return [
            ACTIVITY_HOME,
            *build_source_list(data.get("sources"), data.get("apps")),
        ]

    async def async_turn_on(self, activity: str | None = None, **kwargs: Any) -> None:
        """Turn the TV on and optionally start an activity."""
        await self.coordinator.async_turn_on()
        if activity == ACTIVITY_HOME:
            # "Home" is the launcher, not an app - navigate there via the key.
            await self.coordinator.async_send_key(get_key("home"))
        elif activity:
            # An activity may be an input source or an app. Check sources first and
            # switch input using the TV's own source id; otherwise launch the app.
            data = self.coordinator.data or {}
            source_id = resolve_source_id(activity, data.get("sources"))
            if source_id:
                await self.coordinator.async_select_source(source_id)
                return
            await self.coordinator.async_launch_app(activity)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the TV off."""
        await self.coordinator.async_turn_off()

    async def async_send_key_service(self, key: str) -> None:
        """Handle the vidaa_tv.send_key service for THIS TV."""
        _LOGGER.debug("send_key service: %s", key)
        await self.coordinator.async_send_key(key)

    async def async_launch_app_service(self, app: str) -> None:
        """Handle the vidaa_tv.launch_app service for THIS TV."""
        _LOGGER.debug("launch_app service: %s", app)
        await self.coordinator.async_launch_app(app)

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send remote commands.

        Supports all keys from the pyvidaa library including:
        - Navigation: up, down, left, right, ok, enter, select
        - Menu: back, return, menu, home, exit
        - Volume: volumeup, volup, vol+, volumedown, voldown, vol-, mute
        - Playback: play, pause, stop, forward, ff, rewind, rw
        - Numbers: 0-9
        - Channels: channelup, chup, ch+, channeldown, chdown, ch-
        - Colors: red, green, yellow, blue
        - Extras: info, subtitle, sub, power
        - Mouse: mouse, zoomin, zoomout
        """
        num_repeats = kwargs.get("num_repeats", 1)
        delay_secs = kwargs.get("delay_secs", 0.2)

        for _ in range(num_repeats):
            for cmd in command:
                # Use the library's key mapping which supports all keys
                key = get_key(cmd)
                await self.coordinator.async_send_key(key)

                if delay_secs > 0:
                    await asyncio.sleep(delay_secs)

    async def async_learn_command(self, **kwargs: Any) -> None:
        """Learn a command (not supported)."""
        _LOGGER.warning("Learning commands is not supported on Hisense TV")

    async def async_delete_command(self, **kwargs: Any) -> None:
        """Delete a command (not supported)."""
        _LOGGER.warning("Deleting commands is not supported on Hisense TV")
