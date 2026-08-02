"""Status sensor for the Kilowattlas – Trace the Sun integration.

The integration is a background push service (it creates no measurement
entities of its own), so this sensor gives the user visible feedback that it is
working: connection status, when data was last sent, and how many 15-min slots
are buffered waiting to be sent.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, resolve_map_base
from .coordinator import KilowattlasCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status + production sensors for a config entry."""
    coordinator: KilowattlasCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            KilowattlasStatusSensor(coordinator, entry),
            KilowattlasProductionSensor(coordinator, entry),
        ]
    )


# Human-readable labels for the coordinator's internal status codes.
_STATUS_LABELS = {
    "starting": "Starting",
    "ok": "Connected",
    "revoked": "Disconnected (access revoked)",
    "error": "Connection error",
}


class KilowattlasStatusSensor(SensorEntity):
    """Shows whether production is being shared, and when it was last sent."""

    _attr_has_entity_name = True
    _attr_name = "Sharing status"
    _attr_icon = "mdi:solar-power"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_STATUS_LABELS.values())

    def __init__(
        self, coordinator: KilowattlasCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator state changes."""
        self._coordinator.add_listener(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        return _STATUS_LABELS.get(self._coordinator.status, "Unknown")

    @property
    def extra_state_attributes(self) -> dict:
        last = self._coordinator.last_push
        return {
            "last_sent": last.isoformat() if last else None,
            "buffered_slots": self._coordinator.pending_count,
            "last_batch_accepted": self._coordinator.last_accepted,
            "site_id": self._coordinator.site_id,
            # The cadence actually in effect. Without these the only way to tell
            # whether a config change reached this donor is to read the HA log.
            "slot_seconds": self._coordinator.slot_seconds,
            "push_interval_seconds": self._coordinator.push_interval_seconds,
            "sample_interval_seconds": self._coordinator.sample_interval_seconds,
            "config_version": self._coordinator.config_version,
            # What the sensor is measured to deliver, vs. how fast we poll it.
            # None until a probe concludes (it never concludes on a dark panel).
            "measured_sample_interval_seconds": (
                self._coordinator.measured_sample_interval_seconds
            ),
            # Non-zero means reads are happening but returning unavailable /
            # unknown / non-numeric — the signal for a misconfigured sensor,
            # which was previously silent.
            "unusable_samples_this_slot": (
                self._coordinator.unusable_samples_this_slot
            ),
            # Deep-link to the public map, centred on this installation's
            # position (HA's home coordinates, which seeded the site).
            "map_url": _map_url(self.hass),
        }


class KilowattlasProductionSensor(SensorEntity):
    """Live solar production being shared (kW). Gives HA a native history graph."""

    _attr_has_entity_name = True
    _attr_name = "Shared production"
    _attr_icon = "mdi:white-balance-sunny"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 3

    def __init__(
        self, coordinator: KilowattlasCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_production"
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._coordinator.add_listener(self._on_update)

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._coordinator.current_power_kw


def _device_info(entry: ConfigEntry) -> dict:
    """Group all entities under one device so it reads as one installation."""
    return {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": "Kilowattlas – Trace the Sun",
        "manufacturer": "Kilowattlas",
        "model": "Solar sharing",
    }


def _map_url(hass: HomeAssistant) -> str:
    """Public map URL centred on this HA instance's home position."""
    base = resolve_map_base()
    lat = hass.config.latitude
    lng = hass.config.longitude
    if lat is None or lng is None:
        return base
    return f"{base}/?lat={lat}&lng={lng}&zoom=13"
