"""Sensor platform for Hisense TV.

Surfaces values the integration already fetches but previously kept internal:
the tuned channel, the CEC device name on the active input, and firmware /
chipset details useful for diagnostics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VidaaTVEntity

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VidaaSensorDescription(SensorEntityDescription):
    """Describes a Hisense TV sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    # Optional: extra attributes, for payloads too large for a state value
    # (Home Assistant caps sensor states at 255 characters).
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS: tuple[VidaaSensorDescription, ...] = (
    VidaaSensorDescription(
        key="channel_name",
        translation_key="channel_name",
        name="Channel",
        icon="mdi:television-classic",
        value_fn=lambda data: data.get("channel_name"),
    ),
    VidaaSensorDescription(
        key="channel_num",
        translation_key="channel_num",
        name="Channel number",
        icon="mdi:numeric",
        value_fn=lambda data: data.get("channel_num"),
    ),
    VidaaSensorDescription(
        key="source_detail",
        translation_key="source_detail",
        name="Connected device",
        icon="mdi:hdmi-port",
        # displayname2 - the CEC-reported device on the active input,
        # e.g. "Fire TV Stick" on HDMI3.
        value_fn=lambda data: data.get("source_detail"),
    ),
    VidaaSensorDescription(
        key="statetype",
        translation_key="statetype",
        name="Activity state",
        icon="mdi:state-machine",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.get("statetype"),
    ),
    VidaaSensorDescription(
        key="last_state_payload",
        translation_key="last_state_payload",
        name="Last state response",
        icon="mdi:code-json",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # The state value is just the statetype (a sensor state cannot exceed
        # 255 chars); the full decoded payloads live in the attributes.
        value_fn=lambda data: (data.get("state") or {}).get("statetype"),
        attrs_fn=lambda data: {
            # Last ui_service/state frame - broadcast-driven, so this is the most
            # recent activity change the TV announced.
            "state": data.get("state") or {},
            # Last platform_service/data/gettvinfo reply - the live query that
            # drives power state (fake_sleep_state).
            "tv_info": data.get("tv_info") or {},
        },
    ),
    VidaaSensorDescription(
        key="chipplatform",
        translation_key="chipplatform",
        name="Chipset",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: (data.get("tv_info") or {}).get("chipplatform"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hisense TV sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        VidaaTVSensor(coordinator, entry, description) for description in SENSORS
    )


class VidaaTVSensor(VidaaTVEntity, SensorEntity):
    """A read-only value derived from the coordinator's data."""

    entity_description: VidaaSensorDescription

    def __init__(self, coordinator, entry, description: VidaaSensorDescription) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, entry)
        self.entity_description = description
        base = self._device_id or entry.entry_id
        self._attr_unique_id = f"{base}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the full payload for sensors that carry one."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data or {})
