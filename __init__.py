"""Stove Controller integration."""

import logging
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: Final = ["switch", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Stove Controller from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}

    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Sync demand state: the switch entity is the source of truth.
    # Try to sync immediately, but also handle it in sensor.async_added_to_hass
    # for cases where platforms aren't ready yet
    store = hass.data[DOMAIN][entry.entry_id]
    switch = store.get("switch")
    sensor = store.get("sensor")
    if switch is not None and sensor is not None:
        try:
            await sensor.sync_demand(switch.is_on, switch.entity_id)
        except Exception as e:
            _LOGGER.debug(
                ("Failed to sync demand state on setup "
                 "(will retry in sensor.async_added_to_hass): %s"),
                e,
            )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
