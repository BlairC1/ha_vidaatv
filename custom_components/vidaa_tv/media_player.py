"""Media Player platform for Hisense TV."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player import (
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaType,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import VidaaTVDataUpdateCoordinator
from .entity import VidaaTVEntity
from .helpers import (
    app_icon_url,
    build_source_list,
    find_app,
    resolve_source_id,
)

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hisense TV media player from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([VidaaTVMediaPlayer(coordinator, entry)])


class VidaaTVMediaPlayer(VidaaTVEntity, MediaPlayerEntity):
    """Representation of a Hisense TV media player."""

    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_has_entity_name = True
    _attr_name = None  # Use device name

    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
    )

    def __init__(
        self,
        coordinator: VidaaTVDataUpdateCoordinator,
        entry: VidaaTVConfigEntry,
    ) -> None:
        """Initialize the media player."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = (
            f"{self._device_id}_media_player" if self._device_id else entry.entry_id
        )

    @property
    def available(self) -> bool:
        """Return if entity is available.

        Always available so power button works for WoL even when TV is off.
        """
        return True

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the TV."""
        if not self.coordinator.data or not self.coordinator.available:
            return MediaPlayerState.OFF

        if not self.coordinator.data.get("is_on"):
            return MediaPlayerState.OFF

        # Deliberately ON, never PLAYING.
        #
        # Verified by capturing the broadcast topics while playing a TV-native
        # app (Plex) and live TV: neither payload carries playback data, and no
        # broadcast is sent during playback at all - the state topic only fires
        # on app launch and source change.
        #
        #     {"statetype":"app","name":"plex","url":"...","appId":"42"}
        #     {"statetype":"livetv","channel_name":"9GemHD Melbourne",...}
        #
        # The playstate/curtime/totaltime fields that appear in the fake_sleep
        # payload belong to a generic schema this firmware never populates; they
        # are always zero. So playback state is not observable for HDMI sources,
        # native apps or live TV, and reporting PLAYING would be a guess.
        #
        # Home Assistant only renders transport controls (skip forward/back)
        # when the state is PLAYING or PAUSED, so some TV integrations report it
        # regardless to expose those buttons. That trades an honest state for
        # two controls. The transport methods below are still callable as
        # services and through the remote entity.
        return MediaPlayerState.ON

    @property
    def volume_level(self) -> float | None:
        """Return volume level (0.0 to 1.0)."""
        if not self.coordinator.data:
            return None

        volume = self.coordinator.data.get("volume")
        if volume is not None:
            return volume / 100.0
        return None

    @property
    def is_volume_muted(self) -> bool | None:
        """Return if volume is muted."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("is_muted", False)

    @property
    def media_title(self) -> str | None:
        """Title of current playing media.

        Live TV reports the programme name when the TV knows it, otherwise the
        channel name; app playback reports the app name.
        """
        if not self.coordinator.data:
            return None
        # Key off the retained channel info, not statetype: the TV's `livetv`
        # broadcast is transient and the cached statetype has usually moved on.
        if self.coordinator.data.get("channel_name"):
            return (
                self.coordinator.data.get("program")
                or self.coordinator.data.get("channel_name")
            )
        # Otherwise: the running app, else the CEC device name reported on the
        # active input (displayname2, e.g. "Fire TV Stick" on HDMI3).
        return (
            self.coordinator.data.get("app")
            or self.coordinator.data.get("source_detail")
        )

    @property
    def media_image_url(self) -> str | None:
        """Icon of the running app, if the TV gave us one.

        applist entries carry an ``httpIcon`` field that we already fetch but
        never used; it embeds a real image URL after a data-URI prefix.
        """
        data = self.coordinator.data or {}
        app_name = data.get("app")
        if not app_name:
            return None
        return app_icon_url(find_app(app_name, data.get("apps")))

    @property
    def media_channel(self) -> str | None:
        """Channel currently tuned, e.g. "7Bravo Melbourne"."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("channel_name")

    @property
    def media_content_type(self) -> str | None:
        """Content type, so the frontend renders channel info correctly."""
        if not self.coordinator.data:
            return None
        if self.coordinator.data.get("channel_name"):
            return MediaType.CHANNEL
        if self.coordinator.data.get("app"):
            return MediaType.APP
        return None

    @property
    def source(self) -> str | None:
        """Return current source."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("source")

    @property
    def source_list(self) -> list[str] | None:
        """Inputs + apps, derived from coordinator data (no per-entity fetch).

        The coordinator queries both every poll and publishes them, so there is
        nothing to fetch or cache here - which removes the races that used to
        make inputs or apps vanish from the dropdown.
        """
        data = self.coordinator.data or {}
        names = build_source_list(data.get("sources"), data.get("apps"))
        return names or None

    @property
    def app_name(self) -> str | None:
        """Return current app name."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("app")

    async def async_turn_on(self) -> None:
        """Turn the TV on."""
        await self.coordinator.async_turn_on()

    async def async_turn_off(self) -> None:
        """Turn the TV off."""
        await self.coordinator.async_turn_off()

    async def async_volume_up(self) -> None:
        """Increase volume."""
        await self.coordinator.async_volume_up()

    async def async_volume_down(self) -> None:
        """Decrease volume."""
        await self.coordinator.async_volume_down()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute the volume."""
        await self.coordinator.async_mute()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level (0.0 to 1.0)."""
        await self.coordinator.async_set_volume(int(volume * 100))

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        data = self.coordinator.data or {}

        # Apps first: an app name is launched, not switched to.
        if find_app(source, data.get("apps")):
            await self.coordinator.async_launch_app(source)
            return

        # Otherwise treat as an input source. source_list holds DISPLAY names
        # (e.g. "Onkyo AVR"), but the TV expects its own source id ("HDMI3"), so
        # map back before sending. Inputs whose display name equals their id
        # (HDMI2, HDMI4) worked without this; named ones did not.
        sources = data.get("sources")
        source_id = resolve_source_id(source, sources)
        if source_id:
            _LOGGER.debug("select_source %r -> sourceid %r", source, source_id)
            await self.coordinator.async_select_source(source_id)
            return

        _LOGGER.warning(
            "select_source %r matched no known input; known=%s",
            source,
            [(x.get("displayname"), x.get("sourceid"))
             for x in (sources or []) if isinstance(x, dict)],
        )
        await self.coordinator.async_select_source(source)

    async def async_media_play(self) -> None:
        """Send play command."""
        await self.coordinator.async_send_key("KEY_PLAY")

    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self.coordinator.async_send_key("KEY_PAUSE")

    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self.coordinator.async_send_key("KEY_STOP")

    async def async_media_next_track(self) -> None:
        """Skip forward.

        Tested on 65E86GEVS (HDMI -> AVR -> Fire TV): KEY_FAST_FORWARD and
        KEY_REWIND are NOT actioned, while KEY_RIGHT/KEY_LEFT are - streaming
        apps map the directional keys to seek during playback. So these send the
        directional keys, which work, rather than the transport keys, which do
        nothing. Swap back to KEY_FAST_FORWARD/KEY_REWIND if your TV plays media
        itself and honours them.
        """
        await self.coordinator.async_send_key("KEY_RIGHT")

    async def async_media_previous_track(self) -> None:
        """Skip backward (see async_media_next_track for why KEY_LEFT)."""
        await self.coordinator.async_send_key("KEY_LEFT")


    async def async_browse_media(
        self,
        media_content_type: str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse the TV's installed apps.

        Lets apps be launched from the media browser (and picked up by
        media_player.play_media with media_content_type "app").
        """
        apps = (self.coordinator.data or {}).get("apps") or []
        children = [
            BrowseMedia(
                title=app.get("name"),
                media_class=MediaClass.APP,
                media_content_type=MediaType.APP,
                media_content_id=app.get("name"),
                can_play=True,
                can_expand=False,
                thumbnail=app_icon_url(app),
            )
            for app in apps
            if isinstance(app, dict) and app.get("name")
        ]
        return BrowseMedia(
            title="Apps",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.APPS,
            media_content_id="apps",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.APP,
        )

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media - used for launching apps."""
        if media_type == "app":
            await self.coordinator.async_launch_app(media_id)
        elif media_type == "channel":
            # Could implement channel switching here
            pass
