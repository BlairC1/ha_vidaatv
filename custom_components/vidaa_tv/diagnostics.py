"""Diagnostics support for Hisense TV.

Includes a summary of the protocol behaviour this integration relies on. Modern
VIDAA firmware differs from the older community documentation in several ways
that are easy to misdiagnose, so the map is emitted alongside the live state to
make bug reports self-contained.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import VidaaTVConfigEntry

TO_REDACT = {
    "mac",
    "device_id",
    "deviceid",
    "featurecode",
    "certfile",
    "keyfile",
    "host",
    "ip",
    "network_type",
    "eth0",
    "wlan0",
    "wifi_mac",
}

# What this firmware generation actually supports, established by probing the
# TVs directly (70 candidate actions tested; only these five answer).
PROTOCOL_MAP: dict[str, Any] = {
    "queryable_actions": {
        "ui_service/sourcelist": "inputs; active one has is_signal == '1'",
        "ui_service/applist": "installed apps (includes httpIcon)",
        "ui_service/capability": "UI/firmware versions, resolution, feature flags",
        "platform_service/getdeviceinfo": "model, name, IP, MACs, firmware",
        "platform_service/gettvinfo": (
            "fake_sleep_state (1=on, 0=off), deviceid, chipset, hardware MAC"
        ),
    },
    "unanswered_actions": {
        "ui_service/gettvstate": (
            "never replies on this firmware; state arrives only via broadcasts"
        ),
        "platform_service/getvolume": "never replies; volume arrives via broadcasts",
    },
    "broadcast_topics": {
        "/remoteapp/mobile/broadcast/ui_service/state": "activity changes",
        "/remoteapp/mobile/broadcast/platform_service/actions/volumechange": (
            "volume_type 0=TV speakers, 1=ARC/external amp, 2=mute"
        ),
    },
    "statetypes": {
        "fake_sleep_0": "off / standby",
        "fake_sleep_1": "awake, panel may be off (nightly maintenance wake)",
        "sourceswitch": "on an input",
        "livetv": "tuned to a channel (carries channel_name/channel_num)",
        "app": "app running",
        "remote_launcher": "home screen",
        "remote_setting": "settings menu",
        "remote_epg": "programme guide",
    },
    "notes": [
        "Power state must come from gettvinfo.fake_sleep_state; the cached "
        "statetype is poisoned by a fake_sleep_* frame the TV pushes on connect.",
        "changesource needs the STRING source id from sourcelist ('HDMI3'), not "
        "the numeric id older firmware used.",
        "There is no absolute-volume command over ARC: the TV relays step-based "
        "CEC key presses, so volume_set is emulated by stepping.",
        "Audio-only / picture-off is not remotely controllable on this firmware.",
        "device_id is not always a MAC; Wake-on-LAN must use eth0/wifi_mac.",
    ],
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VidaaTVConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = entry.runtime_data
    coordinator = runtime_data.coordinator
    tv = runtime_data.tv

    device_info = None
    if tv and tv.is_connected:
        try:
            device_info = await tv.async_get_device_info(timeout=5)
        except Exception:  # noqa: BLE001 - diagnostics must never raise
            pass

    data = dict(coordinator.data or {})
    # Trim the bulky lists to counts plus a compact summary; the full app list
    # can run to dozens of entries carrying base64 icon blobs.
    sources = data.pop("sources", []) or []
    apps = data.pop("apps", []) or []

    return {
        "config_entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "data": async_redact_data(data, TO_REDACT),
            "device_data": async_redact_data(coordinator.device_data, TO_REDACT),
            "available": coordinator.available,
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
        },
        "sources": {
            "count": len(sources),
            "entries": [
                {
                    "sourceid": s.get("sourceid"),
                    "displayname": s.get("displayname"),
                    "displayname2": s.get("displayname2"),
                    "is_signal": s.get("is_signal"),
                    "has_signal": s.get("has_signal"),
                }
                for s in sources
                if isinstance(s, dict)
            ],
        },
        "apps": {
            "count": len(apps),
            "names": [a.get("name") for a in apps if isinstance(a, dict)][:40],
        },
        "tv_connection": {
            "connected": tv.is_connected if tv else False,
            "device_info": (
                async_redact_data(device_info, TO_REDACT) if device_info else None
            ),
        },
        "protocol_map": PROTOCOL_MAP,
    }
