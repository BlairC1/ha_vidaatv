"""Data update coordinator for Hisense TV."""

from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pyvidaa import APPS
from pyvidaa.wol import wake_tv
from .const import DOMAIN, SCAN_INTERVAL, STATE_FAKE_SLEEP, CONF_DEVICE_ID, CONF_HOST

_LOGGER = logging.getLogger(__name__)


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
        # Parsed device info (model, sw_version, name, ip, device_id) cached from
        # the TV's getdeviceinfo; entities build their DeviceInfo from this.
        self.device_data: dict[str, Any] = {}

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

    # Safety cap on emulated volume stepping (see async_set_volume).
    # --- volume stepping (used when audio is routed over ARC/eARC) -------------
    # The TV has no absolute-volume command for an external amp: it relays CEC
    # key presses, and CEC volume control is step-based only. So an absolute
    # volume_set is emulated by sending N presses.
    #
    # _VOLUME_STEP_SIZE = how much ONE press moves the reported volume.
    #   0.5 -> AVRs that step in half units (2 presses per reported unit)
    #   1.0 -> devices that step in whole units (1 press per unit)
    # Set this to match your amp; it is the knob that decides how far you land.
    _VOLUME_STEP_SIZE = 0.5
    _VOLUME_STEP_DELAY = 0.08    # gap between presses; raise if presses get dropped
    _MAX_VOLUME_STEPS = 200      # safety cap on a single volume_set

    # Refresh the access token when it has less than this until expiry.
    _TOKEN_REFRESH_THRESHOLD = 24 * 60 * 60  # 1 day

    async def _async_maybe_refresh_token(self) -> None:
        """Proactively refresh the access token while connected.

        The access token lasts ~7 days; refreshing before it expires keeps a
        continuously-loaded integration authenticated without an HA restart or
        reload. A successful refresh persists a new token, so the expiry check
        stops firing afterwards.
        """
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

        import json

        previous = client.on_message

        def _hook(c, userdata, msg):
            try:
                if "volumechange" in msg.topic or "/volume" in msg.topic:
                    payload = json.loads(msg.payload.decode("utf-8", "replace"))
                    vtype = int(payload.get("volume_type", 0))
                    vval = int(payload.get("volume_value", 0))
                    import time as _t
                    self._live_volume_ts = _t.monotonic()
                    if vtype in (0, 1):
                        self._live_volume = vval
                        self._live_volume_type = vtype
                    elif vtype == 2:
                        self._live_muted = bool(vval)
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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from TV."""
        import asyncio, time
        start = time.monotonic()

        try:
            # Check connection
            if not self.tv.is_connected:
                _LOGGER.debug("TV disconnected, rebuilding client and reconnecting")
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
            

            
            # Get current state
            state_start = time.monotonic()
            state = await self.tv.async_get_state(timeout=0.5)
            _LOGGER.debug("get_state took %.2fs, raw state: %s", time.monotonic() - state_start, state)
            # NOTE: this firmware never answers gettvstate; the call returns the
            # cached broadcast/connect-push value, so a long timeout only wastes time.

            # --- live source query (sourcelist answers; gettvstate does not) ----
            # Verified: get_sources() replies in ~0.5s on
            #   /remoteapp/mobile/<client>/ui_service/data/sourcelist
            # and marks the SELECTED input with is_signal == "1" (the flag follows
            # the selection even to an input with nothing plugged in). This is a
            # real on-demand query, so the source stays correct even when the
            # one-shot broadcast the TV sends at power-on is missed.
            active_source = None
            try:
                src_start = time.monotonic()
                sources = await self.tv.async_get_sources(timeout=6)
                if sources:
                    for s_ in sources:
                        if str(s_.get("is_signal")) == "1":
                            active_source = s_.get("displayname") or s_.get("sourcename")
                            break
                _LOGGER.debug("get_sources took %.2fs, active source: %s",
                              time.monotonic() - src_start, active_source)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("get_sources failed: %s", err)
            # --- end live source query ------------------------------------------

            # Determine power state
            is_on = True
            if state:
                # Verified by probing a TV in both states:
                #   fake_sleep_0 = off / standby
                #   fake_sleep_1 = AWAKE but panel off (nightly maintenance wake,
                #                  audio-only mode, or simply just-connected)
                #   sourceswitch / livetv / app / remote_launcher = on and in use
                #
                # OBSERVATION MODE: only fake_sleep_0 counts as off for now, so
                # behaviour is unchanged while we gather 2am data. If the overnight
                # log shows the maintenance wake (fake_sleep_1) with
                # volume_answered=False, switch this to a startswith("fake_sleep")
                # test and the volume probe below will still rescue a genuinely-on
                # TV that is momentarily reporting fake_sleep_1.
                if state.get("statetype") == STATE_FAKE_SLEEP:
                    is_on = False
            else:
                # No state response - TV might be off or unreachable
                is_on = False

            # Get volume and mute status (only if TV is on)
            # Note: getvolume request may not work on all TVs, but volume is cached
            # from volumechange broadcasts when user changes volume
            volume = None
            is_muted = False

            # Probe volume unconditionally: publishing getvolume makes an ON TV
            # broadcast its volume, so a fresh broadcast is a live "TV is on"
            # signal. (Do not gate this on is_on - that is what we are testing.)
            probe_before = self._live_volume_ts
            try:
                vol_start = time.monotonic()
                await self.tv.async_get_volume(timeout=1.0)
                is_muted = self.tv.is_muted
                _LOGGER.debug("get_volume probe took %.2fs", time.monotonic() - vol_start)
            except Exception as err:
                _LOGGER.debug("get_volume failed: %s", err)

            volume_answered = self._live_volume_ts > probe_before
            if volume_answered:
                # A TV in standby does not emit volume broadcasts, so this is a
                # positive power signal. Additive only: it can promote to on, never
                # demote, so a wrong guess here cannot make the state worse.
                if not is_on:
                    _LOGGER.debug(
                        "Volume broadcast received; treating TV as on "
                        "(cached statetype was %s)", state.get("statetype") if state else None,
                    )
                is_on = True

            if self._live_volume is not None:
                volume = self._live_volume
            if self._live_muted:
                is_muted = True

            # Build data dict
            # State contains 'statetype' which indicates current activity:
            # - 'app': running an app (has 'name', 'url', 'appId' fields)
            # - 'sourceswitch': watching a source (has 'sourceid', 'sourcename' fields)
            # - 'remote_launcher': at home screen
            # - 'fake_sleep_0': TV is off/sleeping
            statetype = state.get("statetype") if state else None

            # Extract current app or source based on statetype
            app = None
            source = None
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

            data = {
                "is_on": is_on,
                "state": state,
                "statetype": statetype,
                "volume": volume,
                "is_muted": is_muted,
                "app": app,
                "source": source,
            }

            _LOGGER.debug(
                "State data: is_on=%s, statetype=%s, volume=%s, app=%s, source=%s, "
                "volume_answered=%s",
                is_on, statetype, volume, app, source, volume_answered,
            )
            _LOGGER.debug("Total update took %.2fs", time.monotonic() - start)
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
            self.entry.options.get("wol_mac")
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

        The absolute ``changevolume`` command is ignored when audio is routed over
        ARC/eARC to an external amp (the TV only applies it to its own speakers),
        so step to the target with volume_up/volume_down instead. The live volume
        captured from the broadcasts (see _attach_volume_listener) gives us the
        current level to compute the delta from. Falls back to the absolute command
        if we have not seen a volume broadcast yet.
        """
        import asyncio

        target = max(0, min(100, int(volume)))
        current = self._live_volume

        # The TV applies absolute changevolume to its OWN speakers (volume_type 0),
        # where it works fine. Only emulate with stepping when audio is routed to an
        # external amp over ARC/eARC (volume_type 1), where absolute is ignored.
        if self._live_volume_type != 1 or current is None:
            _LOGGER.debug("Absolute volume set to %s (output type=%s)",
                          target, self._live_volume_type)
            await self.tv.async_set_volume(target)
            await self.async_request_refresh()
            return

        delta = target - int(current)
        if delta == 0:
            return

        # Open loop: send a fixed number of presses. The TV's volume broadcasts lag
        # too much to steer off them reliably, so we compute the press count up
        # front from the amp's step size (see _VOLUME_STEP_SIZE).
        # UI option wins; falls back to the class default.
        try:
            step_size = float(
                self.entry.options.get("volume_step_size", self._VOLUME_STEP_SIZE)
            )
        except (TypeError, ValueError):
            step_size = self._VOLUME_STEP_SIZE
        if step_size <= 0:
            step_size = 1.0
        presses = int(round(abs(delta) / step_size))
        presses = max(1, min(presses, self._MAX_VOLUME_STEPS))

        step = self.tv.async_volume_up if delta > 0 else self.tv.async_volume_down
        _LOGGER.debug(
            "Stepping volume %s -> %s: %s presses %s (step size %s)",
            current, target, presses, "up" if delta > 0 else "down", step_size,
        )
        for _ in range(presses):
            await step()
            await asyncio.sleep(self._VOLUME_STEP_DELAY)

        await self.async_request_refresh()

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
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
        """Get available apps."""
        return await self.tv.async_get_apps()

    async def async_get_sources(self) -> list[dict] | None:
        """Get available sources."""
        return await self.tv.async_get_sources()
