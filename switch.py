"""Switch platform for the Stove Controller integration.

Exposes a demand switch that the VTherm central boiler toggles directly,
replacing the external input_boolean.stove_demand helper.
"""

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, STOVE_DEMAND_CHANGED

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the Stove Demand switch."""
    switch = StoveDemandSwitch(entry.entry_id)
    # Store reference for backwards compatibility with sync_demand
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    store["switch"] = switch
    async_add_entities([switch])


class StoveDemandSwitch(SwitchEntity, RestoreEntity):
    """Switch entity representing stove demand.

    VTherm (or any caller) toggles this switch to request heat.
    The controller sensor applies anti-short-cycle logic before
    forwarding the command to the physical relay.
    """

    def __init__(self, entry_id: str) -> None:
        """Initialize the demand switch."""
        self._entry_id = entry_id
        self._attr_name = "Stove Demand"
        self._attr_unique_id = f"{entry_id}_stove_demand"
        self._attr_icon = "mdi:toggle-switch"
        self._attr_should_poll = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Stove Controller",
            manufacturer="Custom",
            model="A251 Controller",
        )
        self._is_on = False

    @property
    def is_on(self) -> bool:
        """Return true if demand is on."""
        return self._is_on

    async def async_added_to_hass(self) -> None:
        """Restore demand state on restart."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._is_on = last_state.state == STATE_ON
        else:
            self._is_on = False

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on demand."""
        self._is_on = True
        self.async_write_ha_state()
        await self._notify_controller()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off demand."""
        self._is_on = False
        self.async_write_ha_state()
        await self._notify_controller()

    async def _notify_controller(self) -> None:
        """Forward the demand change to the controller sensor via event bus."""
        try:
            entity_id = getattr(self, "entity_id", None) or self._attr_unique_id
            self.hass.bus.async_fire(
                STOVE_DEMAND_CHANGED,
                {"entry_id": self._entry_id, "demand_on": self._is_on, "entity_id": entity_id},
            )
        except Exception as e:
            _LOGGER.error(
                "Failed to fire demand change event: %s", e
            )
