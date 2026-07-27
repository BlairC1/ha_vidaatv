"""Data update coordinator for Hisense TV."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pyvidaa import APPS
from pyvidaa.wol import wake_tv
from pyvidaa.topics import TOPIC_SET_SOURCE, get_topic
from .const import (
    CONF_DEVICE_ID,
    DEFAULT_PORT,
    CONF_PORT,
    CONF_HOST,
    CONF_HW_MAC,
    DOMAIN,
    SCAN_INTERVAL,
    STATE_FAKE_SLEEP,
)

_LOGGER = logging.getLogger(__name__)


async def _try(coro):
    """Await a query, returning the exception rather than raising.

    Queries are best-effort: one failing must not abort the whole poll.
    """
    try:
        return await coro
    except Exception as err:  # noqa: BLE001 - result is inspected by the caller
        return err


def _ipv4_broadcast_subnet(host: str) -> str | None:
    """Return the /24 subnet prefix (e.g. "10.0.0") for an IPv4 host.

    Returns None for hostnames or IPv6 addresses; wake_tv then falls back to
    the global broadcast address.
    """
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address):
            return host.rsplit(".", 1)[0]
    except ValueError:
        pass
    return None


class VidaaTVDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage data updates from Hisense TV."""

    def __init__(
        self,
        hass: HomeAssistant,
        tv,  # AsyncVidaaTV
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        # Get scan interval from options, with fallback to default
        scan_interval = entry.options.get("scan_interval", SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.tv = tv
        self.entry = entry
        # The user-configured interval, restored whenever the TV is on.
        self._on_interval = timedelta(seconds=scan_interval)
        self._available = True
        self._device_info_fetched = False
        self._auth_failures = 0
        # Volume/mute captured directly from MQTT broadcasts (see
        # _attach_volume_listener); pyvidaa drops the ARC/external-amp type.
        self._live_volume: int | None = None
        self._live_muted: bool = False
        # Which output last reported volume: 0 = TV speakers, 1 = ARC/external amp.
        self._live_volume_type: int | None = None
        # When a volume broadcast last arrived. Publishing getvolume makes a TV
        # that is ON emit one; a TV in standby stays silent. That gives us a LIVE
        # power probe, which the cached statetype cannot provide (the connect-push
        # reports fake_sleep_* regardless of whether the TV is on).
        self._live_volume_ts: float = 0.0
        self._last_is_on: bool | None = None  # last authoritative power state
        self._volume_task = None              # in-flight ARC volume stepping task
        self._volume_target: int | None = None
        self._source_cache: list[dict] = []  # full sourcelist from last good poll
        # Seed from the persisted value so WoL works even if the TV has been
        # in deep standby since before Home Assistant started.
        self._hw_mac: str | None = entry.data.get(CONF_HW_MAC)
        self._tv_info: dict[str, Any] = {}    # last gettvinfo payload
        self._poll_count = 0                  # drives the app-list refresh cadence
        self._settle_polls = 0                # fast polls remaining after power-on
        self._last_seen_on: bool | None = None  # for off->on edge detection
        self._last_token_check: float | None = None  # throttles the saved-token check
        self._apps_cache: list[dict] = []    # full app list from last good fetch
        # Live-TV channel info, captured from the transient `livetv` broadcast.
        # The TV emits livetv then immediately sourceswitch/app, so the cached
        # statetype has usually moved on by the time we poll - we must retain it.
        self._live_channel_name: str | None = None
        self._live_channel_num: str | None = None
        self._live_program: str | None = None
        # Parsed device info (model, sw_version, name, ip, device_id) cached from
        # the TV's getdeviceinfo; entities build their DeviceInfo from this.
        self.device_data: dict[str, Any] = {}

    def _set_poll_cadence(self, *, tv_on: bool) -> None:
        """Poll quickly while the TV is off, and briefly after it comes on."""
        if tv_on and self._settle_polls > 0:
            self._settle_polls -= 1
            wanted = self._OFF_SCAN_INTERVAL
        else:
            wanted = self._on_interval if tv_on else self._OFF_SCAN_INTERVAL
        if self.update_interval != wanted:
            self.update_interval = wanted
            self.vlog(
                "Poll interval -> %ss (TV %s)",
                int(wanted.total_seconds()), "on" if tv_on else "off",
            )

    def vlog(self, msg: str, *args) -> None:
        """Log a verbose line.

        Kept as a named helper for the noisier per-poll lines. Visibility is
        controlled by the 'Debug logging' switch, which sets this integration's
        (and pyvidaa's) logger level - so every debug line is surfaced, not just
        the ones routed through here.
        """
        _LOGGER.debug(msg, *args)

    @property
    def available(self) -> bool:
        """Return if TV is available."""
        return self._available

    async def _async_fetch_device_info(self) -> None:
        """Fetch the TV's device info once and cache it in ``self.device_data``.

        The entities build their ``DeviceInfo`` from this cache. The first
        coordinator refresh runs before the entities/device are created, so the
        cache is ready by the time HA reads ``device_info`` at device creation —
        no after-the-fact device-registry surgery is required (that race is why
        model/firmware previously never showed up).
        """
        if self._device_info_fetched:
            return

        try:
            info = await self.tv.async_get_device_info(timeout=5)
        except Exception as err:
            _LOGGER.debug("Error fetching device info: %s", err)
            return

        if not info:
            # Leave the flag unset so we retry on a later refresh (e.g. the TV
            # was off during setup and comes online afterwards).
            _LOGGER.debug("No device info returned from TV yet")
            return

        self.device_data = {
            "model": info.get("model_name"),
            "sw_version": info.get("tv_version"),
            "name": info.get("tv_name"),
            "ip": info.get("ip"),
            # network_type is the device id (MAC without colons) per project convention.
            "device_id": info.get("network_type"),
        }
        # Capture the hardware MAC for Wake-on-LAN, choosing the interface the TV
        # is ACTUALLY connected on. This matters: a magic packet sent to the
        # wired MAC of a TV that is on Wi-Fi goes nowhere. getdeviceinfo reports
        # both (`eth0`, `wlan0`) plus `network_type` telling us which is live.
        #
        # It is also distinct from device_id: on some models device_id is the
        # opaque gettvinfo `deviceid` string rather than a MAC, which silently
        # disabled WoL entirely.
        network_type = str(info.get("network_type") or "").lower()
        bare = network_type.replace(":", "").replace("-", "")
        network_type_is_mac = len(bare) == 12 and all(
            c in "0123456789abcdef" for c in bare
        )

        if network_type_is_mac:
            # Older firmware (e.g. 40A33EXVT / ...09G) puts the MAC of the
            # ACTIVE interface directly in network_type. Trust it: it already
            # names the live interface, and guessing from eth0/wlan0 here picks
            # the wrong one on a TV whose wired port is unused.
            preferred = network_type
            how = "network_type (active interface)"
        elif any(k in network_type for k in ("wlan", "wifi", "wireless")):
            # Newer firmware (e.g. 65E86GEVS / ...O.P0930) reports a descriptive
            # value instead and lists both interfaces separately.
            preferred = info.get("wlan0") or info.get("wifi_mac") or info.get("eth0")
            how = "wireless"
        else:
            preferred = (
                info.get("eth0") or info.get("mac") or info.get("wlan0")
                or info.get("wifi_mac")
            )
            how = "wired"
        if preferred and preferred != self._hw_mac:
            self._hw_mac = preferred
            _LOGGER.debug(
                "WoL target MAC %s (network_type=%r -> %s)",
                preferred, info.get("network_type"), how,
            )
            # Persist it: this is the only time we can learn it (the TV must be
            # reachable), but WoL needs it precisely when the TV is not.
            # Guarded: saving the MAC is an optimisation, and must never be able
            # to fail the whole refresh.
            try:
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, CONF_HW_MAC: preferred},
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Could not persist WoL MAC: %s", err)
        self._device_info_fetched = True
        _LOGGER.debug("Cached device info: %s", self.device_data)

        # Best-effort: if the device already exists (TV came online after setup),
        # refresh it now so the user need not reload. If it doesn't exist yet
        # (first refresh, before entity setup), that's fine — entity creation
        # applies device_data via DeviceInfo.
        device_registry = dr.async_get(self.hass)
        identifier = self.entry.data.get(CONF_DEVICE_ID) or self.entry.entry_id
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, identifier)}
        )
        if device_entry:
            updates = {}
            if self.device_data["model"] and self.device_data["model"] != device_entry.model:
                updates["model"] = self.device_data["model"]
            if self.device_data["sw_version"] and self.device_data["sw_version"] != device_entry.sw_version:
                updates["sw_version"] = self.device_data["sw_version"]
            if updates:
                device_registry.async_update_device(device_entry.id, **updates)
                _LOGGER.debug("Refreshed existing device %s: %s", device_entry.id, updates)

    # applist is near-static (it only changes when apps are installed/removed),
    # so refresh it every N polls rather than on every cycle.
    _APP_REFRESH_EVERY = 20

    # Safety cap on emulated volume stepping (see async_set_volume).
    # --- volume stepping (used when audio is routed over ARC/eARC) -------------
    # The TV has no absolute-volume command for an external amp: it relays CEC
    # key presses, and CEC volume control is step-based only. So an absolute
    # volume_set is emulated by sending N presses.
    #
    # Each press waits for the TV to acknowledge it with a volume broadcast, so
    # there is no fixed inter-press delay to tune - the loop runs at whatever
    # rate the amp actually manages (~0.75s/step measured). These only bound the
    # failure cases.
    # Reachability probe: a refused connection fails in ms. A TV that black-holes
    # instead runs to this timeout, so keep it short - a TV that is actually awake
    # answers on the LAN in milliseconds.
    _PROBE_TIMEOUT = 0.5

    # While the TV is off we only run the cheap probe, so we can afford to check
    # far more often than the normal poll interval. This is what makes a power-on
    # show up in seconds instead of waiting out a full cycle. Once the TV is on we
    # revert to the configured interval, since a real poll is comparatively
    # expensive (~0.5s of queries).
    _OFF_SCAN_INTERVAL = timedelta(seconds=8)

    # After the TV comes on, keep polling quickly for a few cycles. The first
    # poll gets the source (a live query), but volume only arrives via the
    # broadcast that our getvolume call TRIGGERS - it lands a moment after the
    # data dict was built, so it would otherwise not show until the next slow
    # poll. Channel info behaves the same way. A short fast window lets these
    # settle in seconds rather than up to a full interval.
    _SETTLE_POLLS = 3

    _VOLUME_ACK_TIMEOUT = 2.0    # how long to wait for a press to be acknowledged
    _VOLUME_STALL_LIMIT = 3      # consecutive unacknowledged presses before giving up
    _MAX_VOLUME_STEPS = 60       # hard cap on a single volume_set

    # Refresh the access token when it has less than this until expiry.
    _TOKEN_REFRESH_THRESHOLD = 24 * 60 * 60  # 1 day

    # The access token lasts ~7 days and we act 1 day before expiry, so reading
    # the saved-token file every poll was ~2880x more often than useful. Check
    # hourly instead.
    _TOKEN_CHECK_INTERVAL = 60 * 60

    async def _async_maybe_refresh_token(self) -> None:
        """Proactively refresh the access token while connected.

        The access token lasts ~7 days; refreshing before it expires keeps a
        continuously-loaded integration authenticated without an HA restart or
        reload. A successful refresh persists a new token, so the expiry check
        stops firing afterwards.
        """
        now = time.monotonic()
        if (
            self._last_token_check is not None
            and now - self._last_token_check < self._TOKEN_CHECK_INTERVAL
        ):
            return
        self._last_token_check = now

        try:
            status = await self.tv.async_token_status()
            if not status.get("has_token") or status.get("needs_reauth"):
                return
            near_expiry = (
                status.get("access_valid")
                and status.get("access_expires_in", 0) < self._TOKEN_REFRESH_THRESHOLD
            )
            if status.get("needs_refresh") or near_expiry:
                _LOGGER.debug(
                    "Access token near expiry (%ss left), refreshing",
                    status.get("access_expires_in", 0),
                )
                if not await self.tv.async_refresh_token():
                    _LOGGER.debug("Proactive token refresh failed")
        except Exception as err:
            _LOGGER.debug("Token refresh check failed: %s", err)

    def _attach_volume_listener(self) -> None:
        """Tee the MQTT callback to capture volume broadcasts pyvidaa discards.

        Verified on this firmware:
            volume_type 0 = TV internal speaker volume
            volume_type 1 = ARC/eARC external amp volume (AVR / soundbar)
            volume_type 2 = mute state (0 = unmuted, 1 = muted)
        The TV only broadcasts the type for the CURRENTLY ACTIVE output, so with
        audio running through an AVR only type 1 is sent - which pyvidaa ignores,
        leaving volume permanently None. Last-wins is correct because only the
        active output broadcasts.

        The flag lives on the paho client, which async_reset() replaces, so the
        hook re-attaches automatically after every reconnect.
        """
        # self.tv is an AsyncVidaaTV, which lazily wraps a sync VidaaTV; the paho
        # client lives one level deeper again. Either level can be None before the
        # first connect, so walk down and bail out safely if it is not ready yet.
        client = getattr(self.tv, "_client", None)          # AsyncVidaaTV -> VidaaTV
        if client is not None and not hasattr(client, "on_message"):
            client = getattr(client, "_client", None)       # VidaaTV -> paho client
        if client is None or not hasattr(client, "on_message"):
            return
        if getattr(client, "_vidaa_vol_hook", False):
            return

        previous = client.on_message

        def _hook(c, userdata, msg):
            try:
                if "volumechange" in msg.topic or "/volume" in msg.topic:
                    payload = json.loads(msg.payload.decode("utf-8", "replace"))
                    vtype = int(payload.get("volume_type", 0))
                    vval = int(payload.get("volume_value", 0))
                    now = time.monotonic()
                    if vtype in (0, 1):
                        self._live_volume = vval
                        self._live_volume_type = vtype
                    elif vtype == 2:
                        self._live_muted = bool(vval)
                    self._live_volume_ts = now
                elif "ui_service/state" in msg.topic:
                    payload = json.loads(msg.payload.decode("utf-8", "replace"))
                    st = payload.get("statetype")
                    if st == "livetv":
                        self._live_channel_name = payload.get("channel_name") or None
                        self._live_channel_num = payload.get("channel_num") or None
                        self._live_program = payload.get("progname") or None
                    elif st == "sourceswitch" and str(
                        payload.get("sourceid", "")
                    ).upper() != "TV":
                        # Left the tuner - channel info no longer applies.
                        self._live_channel_name = None
                        self._live_channel_num = None
                        self._live_program = None
            except Exception:  # noqa: BLE001 - never break the MQTT callback
                pass
            if previous:
                try:
                    previous(c, userdata, msg)
                except Exception:  # noqa: BLE001
                    pass

        client.on_message = _hook
        client._vidaa_vol_hook = True
        _LOGGER.debug("Volume broadcast listener attached")

    async def _async_is_reachable(self) -> bool:
        """Cheap TCP check: is the TV answering on its control port?

        A full pyvidaa connect against an unreachable TV costs ~3.1s of a SHARED
        executor thread (measured) and is retried every poll while the TV sits in
        deep standby. A bare TCP connect fails in milliseconds against a dead
        host, so we gate the expensive path behind this.

        Deliberately NOT rate-limited: it is cheap enough to run every cycle,
        which is what keeps power-on detection immediate. Backing off the probe
        itself would delay noticing that the TV has woken - the exact
        responsiveness we are trying to protect.
        """
        host = self.entry.data.get(CONF_HOST)
        port = self.entry.data.get(CONF_PORT, DEFAULT_PORT)
        if not host:
            return True  # nothing to probe against; let the normal path decide
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=self._PROBE_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from TV."""
        start = time.monotonic()

        try:
            # Check connection
            if not self.tv.is_connected:
                # Cheap reachability gate first: skip the ~3.1s handshake when the
                # TV is in deep standby, but keep checking every cycle so a wake
                # is noticed immediately.
                probe_start = time.monotonic()
                if not await self._async_is_reachable():
                    self._available = False
                    self._set_poll_cadence(tv_on=False)
                    _LOGGER.debug(
                        "TV unreachable (probe %.0f ms); skipping connect",
                        (time.monotonic() - probe_start) * 1000,
                    )
                    raise UpdateFailed("TV is not reachable (probably off)")
                _LOGGER.debug(
                    "TV reachable (probe %.0f ms), reconnecting",
                    (time.monotonic() - probe_start) * 1000,
                )
                # Rebuild the client so saved-token status is re-evaluated; an
                # expired access token is then refreshed from the refresh token
                # rather than being replayed and rejected.
                try:
                    await self.tv.async_reset()
                except Exception:
                    pass
                # Try to connect with longer timeout for wake-up scenarios
                connected = await self.tv.async_connect(timeout=5)
                if not connected:
                    self._available = False
                    raise UpdateFailed("Failed to connect to TV")
                _LOGGER.debug("Reconnect took %.2fs", time.monotonic() - start)
                # A reconnect can mean the TV rebooted (e.g. a firmware update),
                # so re-fetch device info to pick up a new firmware version.
                self._device_info_fetched = False

            self._available = True

            # Capture volume broadcasts pyvidaa ignores (re-attaches after reconnects).
            try:
                self._attach_volume_listener()
            except Exception as err:  # noqa: BLE001 - must never fail the refresh
                _LOGGER.debug("Could not attach volume listener: %s", err)

            # Renew the access token before it lapses while connected.
            await self._async_maybe_refresh_token()

            # Cache device info on first successful connection
            await self._async_fetch_device_info()

            # NOTE: no periodic resync here, deliberately.
            # This TV pushes a fake_sleep_* frame on every (re)connect regardless of
            # whether it is actually on, which OVERWRITES a good cached state and
            # made is_on report "off" while the TV was on. Since the current source
            # is now queried live via sourcelist (below), there is nothing left that
            # needs a reconnect to refresh - so we keep the connection up and let the
            # TV's change broadcasts maintain the cached state.
            

            
            # Current state, read from the client's cache - NOT via a query.
            # This firmware never answers gettvstate, so async_get_state() simply
            # burned its full timeout (a measured 0.50s of every poll) and then
            # returned this same cached dict, which the broadcast handler keeps
            # up to date. Reading the property is free and identical.
            state = self.tv.state or {}

            # --- live source query (sourcelist answers; gettvstate does not) ----
            # Verified: get_sources() replies in ~0.5s on
            #   /remoteapp/mobile/<client>/ui_service/data/sourcelist
            # and marks the SELECTED input with is_signal == "1" (the flag follows
            # the selection even to an input with nothing plugged in). This is a
            # real on-demand query, so the source stays correct even when the
            # one-shot broadcast the TV sends at power-on is missed.
            # Queries MUST run sequentially. pyvidaa serialises every request
            # through a single shared slot:
            #     self._response_event.clear(); self._last_response = None
            #     ...publish...; self._response_event.wait(timeout)
            # so two concurrent requests clobber each other - the second clears
            # the event the first is waiting on, and whichever reply arrives
            # first satisfies both. Running these under asyncio.gather made
            # sourcelist return in ~0.01s with no data. Do not parallelise.
            #
            #   sourcelist -> current input (is_signal == "1")
            #   gettvinfo  -> authoritative power state (fake_sleep_state)
            #   applist    -> installed apps; near-static, so only refreshed
            #                 every APP_REFRESH_EVERY polls (or while unknown)
            self._poll_count += 1
            want_apps = (
                not self._apps_cache
                or self._poll_count % self._APP_REFRESH_EVERY == 0
            )
            query_start = time.monotonic()

            sources_res = await _try(self.tv.async_get_sources(timeout=6))
            tv_info_res = await _try(self.tv.async_get_tv_info(timeout=5))
            apps_res = await _try(self.tv.async_get_apps()) if want_apps else None

            # getvolume never RETURNS a value on this firmware, but publishing it
            # makes the TV broadcast its current volume, which the MQTT hook
            # captures. That broadcast is the only way volume stays current while
            # nobody is touching the remote - removing this call left volume at
            # None. Keep the timeout short: we want the side effect, not a reply.
            await _try(self.tv.async_get_volume(timeout=0.2))

            # --- current input, from sourcelist ---------------------------------
            # Verified: sourcelist replies in ~0.5s on
            #   /remoteapp/mobile/<client>/ui_service/data/sourcelist
            # and marks the SELECTED input with is_signal == "1" (the flag follows
            # the selection even to an input with nothing plugged in). A real
            # query, so the source stays correct even when the one-shot broadcast
            # the TV sends at power-on is missed.
            active_source = None
            active_source_detail = None
            if isinstance(sources_res, Exception):
                _LOGGER.debug("get_sources failed: %s", sources_res)
            elif sources_res and isinstance(sources_res, list):
                # Retain the full list so entities don't each re-fetch it (that
                # raced and, on slower firmware, kept coming back empty).
                self._source_cache = sources_res
            for src in self._source_cache:
                if str(src.get("is_signal")) == "1":
                    active_source = src.get("displayname") or src.get("sourcename")
                    active_source_detail = src.get("displayname2") or None
                    break

            # --- installed apps -------------------------------------------------
            if apps_res is not None:
                if isinstance(apps_res, Exception):
                    _LOGGER.debug("get_apps failed: %s", apps_res)
                elif apps_res and isinstance(apps_res, list):
                    self._apps_cache = apps_res

            self.vlog(
                "queries took %.2fs (apps refreshed: %s), active source: %s",
                time.monotonic() - query_start, want_apps, active_source,
            )

            # --- authoritative power state: live gettvinfo query -------------
            # gettvinfo answers on a per-client data topic and reports the TV's
            # CURRENT power state in `fake_sleep_state`:
            #     fake_sleep_state 1 = on,  0 = off
            # (`fake_sleep` is a capability flag - it stays 1 either way - do not
            #  use it.) Verified by querying one TV in both states.
            #
            # This is a real query, so unlike the cached `statetype` it cannot be
            # poisoned by the fake_sleep_* frame the TV pushes on every connect.
            # Fall back to the cached statetype only if the query fails.
            is_on = None
            if isinstance(tv_info_res, Exception):
                _LOGGER.debug("gettvinfo failed: %s", tv_info_res)
            elif tv_info_res:
                # Capture the REAL hardware MAC for Wake-on-LAN. Distinct from
                # device_id, which on some models is the opaque gettvinfo
                # `deviceid` string (not a MAC) - that silently disabled WoL.
                # Only a fallback: gettvinfo reports eth0 without saying which
                # interface is live, so never let it override the interface-aware
                # choice made from getdeviceinfo above.
                if not self._hw_mac:
                    self._hw_mac = (
                        tv_info_res.get("eth0")
                        or tv_info_res.get("wifi_mac")
                        or tv_info_res.get("mac")
                    )
                self._tv_info = tv_info_res
                if "fake_sleep_state" in tv_info_res:
                    is_on = str(tv_info_res.get("fake_sleep_state")) == "1"
                    _LOGGER.debug(
                        "fake_sleep_state=%s -> is_on=%s",
                        tv_info_res.get("fake_sleep_state"), is_on,
                    )

            if is_on is not None:
                # gettvinfo answered - this is authoritative. Remember it.
                self._last_is_on = is_on
            else:
                # gettvinfo did NOT answer. This almost always means the
                # connection just dropped, which is exactly when the cached
                # statetype is least trustworthy (a stale fake_sleep_1 frame from
                # a brief maintenance wake would otherwise read as "on"). So do
                # NOT guess from the cache - hold the last authoritative state.
                if self._last_is_on is not None:
                    is_on = self._last_is_on
                    _LOGGER.debug(
                        "gettvinfo unavailable; holding last known is_on=%s", is_on
                    )
                else:
                    # Never had a good reading yet (e.g. first poll while the TV
                    # is unreachable). Only the definitive off frame counts as off;
                    # otherwise report off rather than inventing "on".
                    is_on = bool(state) and state.get("statetype") not in (
                        None, STATE_FAKE_SLEEP, "fake_sleep_1",
                    )
                    _LOGGER.debug(
                        "gettvinfo unavailable and no prior state; is_on=%s", is_on
                    )
            # --- end power state ----------------------------------------------

            # Arm a short fast-poll window when the TV has just come on, so the
            # volume/channel broadcasts that follow the first poll are picked up
            # promptly instead of waiting a full interval.
            if is_on and self._last_seen_on is False:
                self._settle_polls = self._SETTLE_POLLS
                self.vlog("TV powered on; %s fast settle polls", self._SETTLE_POLLS)
            self._last_seen_on = bool(is_on)

            self._set_poll_cadence(tv_on=bool(is_on))

            # Volume and mute come from the MQTT broadcast hook, not a query.
            # getvolume is never answered by this firmware, and is_muted is a
            # plain cached property - so the old probe was pure overhead. (It
            # also is NOT a power signal: measured on both TVs while OFF, a TV
            # in standby still emits volume broadcasts.)
            volume = self._live_volume
            is_muted = self._live_muted or self.tv.is_muted

            # Build data dict
            # State contains 'statetype' which indicates current activity:
            # - 'app': running an app (has 'name', 'url', 'appId' fields)
            # - 'sourceswitch': watching a source (has 'sourceid', 'sourcename' fields)
            # - 'remote_launcher': at home screen
            # - 'fake_sleep_0': TV is off/sleeping
            statetype = state.get("statetype")

            # Extract current app or source based on statetype
            app = None
            source = None
            channel_name = None
            channel_num = None
            program = None
            if state:
                if statetype == "app":
                    app_key = state.get("name", "").lower()
                    # Get human-readable name from library's APPS dict
                    if app_key in APPS:
                        app = APPS[app_key].get("name", app_key)
                    else:
                        # Fallback: capitalize first letter
                        app = state.get("name", "").capitalize()
                elif statetype == "sourceswitch":
                    source = state.get("displayname") or state.get("sourcename")

            # The live sourcelist query wins over the (possibly stale) broadcast.
            if active_source:
                source = active_source

            # Channel info comes from the retained livetv broadcast (the cached
            # statetype has usually moved on to sourceswitch/app by poll time).
            source_detail = active_source_detail
            channel_name = self._live_channel_name
            channel_num = self._live_channel_num
            program = self._live_program

            data = {
                "is_on": is_on,
                "state": state,
                "statetype": statetype,
                "volume": volume,
                "is_muted": is_muted,
                "app": app,
                "source": source,
                "source_detail": source_detail,
                "volume_type": self._live_volume_type,
                "sources": list(self._source_cache),
                "tv_info": dict(self._tv_info),
                "apps": list(self._apps_cache),
                "channel_name": channel_name,
                "channel_num": channel_num,
                "program": program,
            }

            self.vlog(
                "State data: is_on=%s, statetype=%s, volume=%s, app=%s, source=%s",
                is_on, statetype, volume, app, source,
            )
            self.vlog("Total update took %.2fs", time.monotonic() - start)
            return data

        except Exception as err:
            self._available = False
            # Check for auth-related errors that should trigger reauth
            error_str = str(err).lower()
            if "auth" in error_str or "unauthorized" in error_str or "forbidden" in error_str:
                self._auth_failures += 1
                if self._auth_failures >= 3:
                    _LOGGER.warning("Multiple auth failures, triggering reauthentication")
                    raise ConfigEntryAuthFailed(
                        "Authentication failed. Please re-pair with the TV."
                    ) from err
            raise UpdateFailed(f"Error communicating with TV: {err}") from err

    async def async_turn_on(self) -> None:
        """Turn TV on using WoL and power command."""
        # Resolve the WoL target MAC: explicit wol_mac option wins, else the TV's
        # hardware MAC stored as device_id (config entry, or the value cached from
        # getdeviceinfo once the TV has been seen online). Normalize to bare hex so
        # a colon/dash-formatted value still works.
        raw_mac = (
            self.entry.options.get("wol_mac")      # explicit override wins
            or self._hw_mac                        # learned this session
            or self.entry.data.get(CONF_HW_MAC)    # persisted from a previous run
            or self.entry.data.get(CONF_DEVICE_ID)
            or self.device_data.get("device_id")
        )
        normalized = (raw_mac or "").replace(":", "").replace("-", "").lower()
        if len(normalized) == 12 and all(c in "0123456789abcdef" for c in normalized):
            mac = ":".join(normalized[i:i+2] for i in range(0, 12, 2))
            # Derive a /24 broadcast subnet only for a real IPv4 host.
            host = self.entry.data.get(CONF_HOST, "")
            subnet = _ipv4_broadcast_subnet(host)
            _LOGGER.debug("Sending WoL to %s", mac)
            await self.hass.async_add_executor_job(wake_tv, mac, subnet)
        else:
            _LOGGER.warning(
                "Skipping Wake-on-LAN: no valid MAC (got %r). Set a 'wol_mac' in the "
                "integration options to enable wake-on-LAN.",
                raw_mac,
            )

        # Also send power on command
        await self.tv.async_power_on()
        await self.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn TV off."""
        await self.tv.async_power_off()
        await self.async_request_refresh()

    async def async_volume_up(self) -> None:
        """Increase volume."""
        await self.tv.async_volume_up()
        await self.async_request_refresh()

    async def async_volume_down(self) -> None:
        """Decrease volume."""
        await self.tv.async_volume_down()
        await self.async_request_refresh()

    async def async_mute(self) -> None:
        """Toggle mute."""
        await self.tv.async_mute()
        await self.async_request_refresh()

    async def async_set_volume(self, volume: int) -> None:
        """Set volume level.

        On TV speakers the absolute command works, so send it directly. On ARC the
        TV ignores absolute and we must step - but HA sliders fire volume_set
        repeatedly while dragging, so we run the stepping in a single cancellable
        background task: a new target cancels the in-flight one and re-aims, so we
        always converge on the LATEST value instead of stacking loops that fight.
        """
        target = max(0, min(100, int(volume)))

        # TV speakers (or no reading yet): absolute works - fire and forget.
        if self._live_volume_type != 1 or self._live_volume is None:
            _LOGGER.debug("Absolute volume set to %s (output type=%s)",
                          target, self._live_volume_type)
            await self.tv.async_set_volume(target)
            await self.async_request_refresh()
            return

        # ARC: remember the newest target and (re)start the single stepper.
        self._volume_target = target
        if self._volume_task and not self._volume_task.done():
            # A stepping run is already going; it will pick up the new target on
            # its next iteration. No need to launch another.
            _LOGGER.debug("Volume target updated to %s (stepper running)", target)
            return
        self._volume_task = self.hass.async_create_task(self._async_step_volume_arc())

    async def _async_step_volume_arc(self) -> None:
        """Step ARC volume to self._volume_target, one confirmed press at a time.

        Why this is deliberately slow: the TV relays each volume key over CEC and
        processes them at its own pace - measured at roughly ONE press every
        0.74s on a 65E86GEVS + Onkyo. Firing presses faster simply queues them,
        and the queue keeps draining long after we stop. A previous version sent
        91 presses in 7.4s to make a 10-step change; the backlog carried the
        volume from 50 past the target of 60 all the way to 100.

        So we never have more than one press in flight: press, wait for the TV to
        broadcast the new level (that broadcast IS the acknowledgement), then
        re-evaluate. This self-calibrates to whatever rate the amp runs at and
        cannot overshoot, at the cost of taking ~0.75s per step.

        For instant, exact volume, control the amplifier's own entity directly -
        an AVR accepts absolute levels natively, which CEC cannot express.
        """
        started = time.monotonic()
        start_volume = self._live_volume
        presses = 0
        _LOGGER.debug(
            "Volume stepping start: live=%s target=%s (one confirmed press at a time)",
            start_volume, self._volume_target,
        )

        # Give up on a press that is never acknowledged, so a missed broadcast
        # cannot wedge the loop forever.
        ack_timeout = self._VOLUME_ACK_TIMEOUT
        stalled = 0

        while presses < self._MAX_VOLUME_STEPS:
            target = self._volume_target
            current = self._live_volume
            if target is None or current is None:
                break

            delta = target - int(current)
            if delta == 0:
                _LOGGER.debug(
                    "Volume reached target %s after %s presses in %.2fs",
                    target, presses, time.monotonic() - started,
                )
                break

            step = self.tv.async_volume_up if delta > 0 else self.tv.async_volume_down
            presses += 1
            before = current
            await step()

            # Wait for the acknowledging broadcast rather than sleeping blindly.
            deadline = time.monotonic() + ack_timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                if self._live_volume != before:
                    break

            if self._live_volume == before:
                stalled += 1
                _LOGGER.debug(
                    "Volume no ack within %.1fs (stall %s/%s), live still %s",
                    ack_timeout, stalled, self._VOLUME_STALL_LIMIT, before,
                )
                if stalled >= self._VOLUME_STALL_LIMIT:
                    _LOGGER.debug(
                        "Volume giving up: volume not responding (at min/max?)"
                    )
                    break
            else:
                stalled = 0
        else:
            _LOGGER.debug("Volume hit MAX_VOLUME_STEPS guard (%s)",
                          self._MAX_VOLUME_STEPS)

        _LOGGER.debug(
            "Volume end: start=%s target=%s final=%s presses=%s elapsed=%.2fs",
            start_volume, self._volume_target, self._live_volume,
            presses, time.monotonic() - started,
        )
        await self.async_request_refresh()

    async def async_select_source(self, source: str) -> None:
        """Select input source.

        pyvidaa maps names through SOURCE_MAP to NUMERIC ids (hdmi3 -> "5"), which
        is what older firmware expects. This firmware uses the STRING ids reported
        by sourcelist ("TV", "HDMI3", "AVS"), and silently ignores the numeric
        form - which is why HDMI selection did nothing while apps worked. So
        publish changesource directly with the id straight from sourcelist, and
        fall back to pyvidaa's mapping if that is not possible.
        """
        try:
            def _publish_raw() -> bool:
                client = getattr(self.tv, "_client", None)   # sync VidaaTV
                if client is None or not hasattr(client, "_publish"):
                    return False
                topic = get_topic(TOPIC_SET_SOURCE, client.client_id)
                _LOGGER.debug("changesource -> %r", source)
                client._publish(topic, {"sourceid": source})
                return True

            if await self.hass.async_add_executor_job(_publish_raw):
                await self.async_request_refresh()
                return
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Raw changesource failed (%s); falling back", err)

        await self.tv.async_set_source(source)
        await self.async_request_refresh()

    async def async_send_key(self, key: str) -> None:
        """Send remote key."""
        await self.tv.async_send_key(key)

    async def async_launch_app(self, app_name: str) -> None:
        """Launch app."""
        await self.tv.async_launch_app(app_name)
        await self.async_request_refresh()

    async def async_get_apps(self) -> list[dict] | None:
        """Get installed apps, caching the last good result."""
        result = await self.tv.async_get_apps()
        if result and isinstance(result, list):
            self._apps_cache = result
        return self._apps_cache or result

    async def async_get_sources(self) -> list[dict] | None:
        """Get available sources.

        Prefer the list captured during the regular poll (self._source_cache):
        it's already fresh, and re-querying here raced with the poll and returned
        empty on slower firmware, which made inputs vanish from the dropdowns.
        Only hit the TV directly if the cache is still empty.
        """
        if self._source_cache:
            return list(self._source_cache)
        result = await self.tv.async_get_sources()
        if result and isinstance(result, list):
            self._source_cache = result
        return result
