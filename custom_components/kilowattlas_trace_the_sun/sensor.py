"""Status sensor for the Kilowattlas – Trace the Sun integration.

The integration is a background push service (it creates no measurement
entities of its own), so this sensor gives the user visible feedback that it is
working: connection status, when data was last sent, and how many 15-min slots
are buffered waiting to be sent.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import KilowattlasCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the status sensor for a config entry."""
    coordinator: KilowattlasCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([KilowattlasStatusSensor(coordinator, entry)])


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
        self._attr_unique_id = f"{entry.entry_id}_status"
        # Group the sensor under a device so it reads as one installation.
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Kilowattlas – Trace the Sun",
            "manufacturer": "Kilowattlas",
            "model": "Solar sharing",
        }

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
        }
