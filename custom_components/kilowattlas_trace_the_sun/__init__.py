"""The Kilowattlas Solar Sharing integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.const import Platform

from .api import KilowattlasClient
from .const import (
    CONF_API_BASE,
    CONF_POWER_SENSOR,
    CONF_SITE_ID,
    CONF_TOKEN,
    DOMAIN,
    resolve_api_base,
)
from .coordinator import KilowattlasCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kilowattlas from a config entry."""
    session = async_get_clientsession(hass)
    # Prefer the base captured at link time; fall back to the env/prod default.
    client = KilowattlasClient(
        session, entry.data.get(CONF_API_BASE) or resolve_api_base()
    )
    coordinator = KilowattlasCoordinator(
        hass,
        client,
        entry.data[CONF_TOKEN],
        entry.data[CONF_POWER_SENSOR],
        entry.data.get(CONF_SITE_ID),
    )
    await coordinator.async_start()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: KilowattlasCoordinator | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if coordinator is not None:
        await coordinator.async_stop()
    return unloaded
