"""The Hisense TV integration."""

from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass, field
from typing import Any


from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_MAC,
    CONF_DEVICE_ID,
    CONF_BRAND,
    CONF_CERTFILE,
    CONF_KEYFILE,
    DEFAULT_PORT,
    PLATFORMS,
)
from .coordinator import VidaaTVDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Import from PyPI package (pyvidaa)
from pyvidaa import AsyncVidaaTV
from pyvidaa.config import get_storage
from pyvidaa.protocol import AuthMethod


@dataclass
class VidaaTVRuntimeData:
    """Runtime data for Hisense TV integration."""

    coordinator: VidaaTVDataUpdateCoordinator
    tv: AsyncVidaaTV
    # The options this entry was set up with, to tell an options change (which
    # needs a reload) from a data-only update (which does not).
    options: dict[str, Any] = field(default_factory=dict)


type VidaaTVConfigEntry = ConfigEntry[VidaaTVRuntimeData]

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

def _stable_mac(seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).digest()
    # First octet 0x02 => locally administered, unicast.
    return "02:" + ":".join(f"{b:02X}" for b in digest[:5])


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Hisense TV integration."""
    await _async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> bool:
    """Set up Hisense TV from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    mac = entry.data.get(CONF_MAC)
    device_id = entry.data.get(CONF_DEVICE_ID)
    brand = entry.data.get(CONF_BRAND, "his")
    certfile = entry.data.get(CONF_CERTFILE)
    keyfile = entry.data.get(CONF_KEYFILE)

    _LOGGER.debug("Setting up Hisense TV at %s:%s", host, port)

    # Create the async TV client
    tv = AsyncVidaaTV(
        host=host,
        port=port,
        certfile=certfile,
        keyfile=keyfile,
        mac_address=mac or device_id or _stable_mac(entry.entry_id),
        use_dynamic_auth=True,
        auth_method=AuthMethod.MODERN,   # skip the ~32s blocking UPnP probe on every reconnect
        brand=brand,
        enable_persistence=True,
    )

    # No connect here. The coordinator's first refresh probes the TV and
    # connects only if it answers, so a TV in deep standby costs a 0.5s probe
    # rather than a multi-second connect attempt on every Home Assistant start.
    # Setup never depends on the TV being on - the entities (including the
    # power control that sends Wake-on-LAN) must exist either way.
    coordinator = VidaaTVDataUpdateCoordinator(hass, tv, entry)
    # Restore the saved source/app lists first so the dropdowns are populated
    # even when the TV is off at startup.
    await coordinator.async_load_cache()
    # async_refresh (not async_config_entry_first_refresh) so an unreachable TV
    # doesn't abort setup; the coordinator reconnects on a later poll.
    await coordinator.async_refresh()

    # Store runtime data using the modern pattern
    entry.runtime_data = VidaaTVRuntimeData(
        coordinator=coordinator, tv=tv, options=dict(entry.options)
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Mirror the amplifier's volume while audio is on ARC (if one is configured).
    entry.async_on_unload(coordinator.async_setup_amp_listener())

    # Register update listener for options
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Service setup.

    ``send_key`` and ``launch_app`` are registered as ENTITY services by the
    remote platform (see remote.py) so Home Assistant resolves the target
    itself. They used to be registered here as domain services with a schema
    that rejected `entity_id`, which made every targeted call fail validation -
    and the handler ignored the target regardless, firing at all TVs at once.
    """
    return


async def async_unload_entry(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        runtime_data = entry.runtime_data
        if runtime_data.tv:
            await runtime_data.tv.async_disconnect()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> None:
    """Clean up the saved source/app lists when a TV is removed."""
    from homeassistant.helpers.storage import Store

    try:
        await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
    except Exception as err:  # noqa: BLE001 - nothing depends on this
        _LOGGER.debug("Could not remove saved lists for %s: %s", entry.title, err)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: VidaaTVConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Allow a device to be removed from the UI.

    Returning True lets HA delete the device from the device page. Each TV is its
    own config entry, so manual removal of a stale device is always permitted.
    """
    return True


async def async_update_options(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> None:
    """Reload when the OPTIONS change.

    Update listeners also fire for data-only updates, and the coordinator makes
    one itself when it first learns the TV's Wake-on-LAN MAC. Reloading for that
    tore the integration down at the very moment the TV had just come online.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.options == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)
