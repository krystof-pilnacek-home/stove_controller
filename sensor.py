"""Sensor platform for the Stove Controller integration."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_RELAY_ENTITY,
    DEFAULT_MIN_OFF_DURATION,
    DEFAULT_MIN_ON_DURATION,
    DOMAIN,
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up the Stove Controller sensor."""
    data = {**entry.data, **(entry.options or {})}
    relay_entity = data[CONF_RELAY_ENTITY]
    min_on_min = data.get(CONF_MIN_ON_DURATION, DEFAULT_MIN_ON_DURATION)
    min_off_min = data.get(CONF_MIN_OFF_DURATION, DEFAULT_MIN_OFF_DURATION)

    sensor = StoveControllerSensor(
        entry.entry_id, relay_entity, min_on_min, min_off_min
    )
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    store["sensor"] = sensor
    async_add_entities([sensor])


class StoveControllerSensor(RestoreEntity, SensorEntity):
    """Sensor that controls the stove relay with anti-short-cycle logic.
    
    Manages a state machine with anti-short-cycle protection:
    - IDLE: No demand, relay off
    - HEATING: Demand on, relay on
    - PENDING_ON: Demand on, but waiting for min_off_duration before turning relay on
    - PENDING_OFF: Demand off, but waiting for min_on_duration before turning relay off
    """

    def __init__(
        self,
        entry_id: str,
        relay_entity: str,
        min_on_min: int,
        min_off_min: int,
    ) -> None:
        """Initialize the sensor."""
        self._entry_id: str = entry_id
        self._relay_entity: str = relay_entity
        self._min_on_duration: int = min_on_min * 60  # Convert minutes to seconds
        self._min_off_duration: int = min_off_min * 60  # Convert minutes to seconds

        self._attr_name = "Stove Controller"
        self._attr_unique_id = f"{entry_id}_stove_controller"
        self._attr_icon = "mdi:fire"
        self._attr_should_poll = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Stove Controller",
            manufacturer="Custom",
            model="A251 Controller",
        )

        self._state: str = STATE_IDLE
        self._demand_on: bool = False
        self._demand_entity_id: Optional[str] = None
        self._last_on: Optional[datetime] = None
        self._last_off: Optional[datetime] = None
        self._wait_until: Optional[datetime] = None
        self._wait_task: Optional[asyncio.Task] = None
        self._update_unsub: Optional[Callable] = None

    @property
    def native_value(self) -> str:
        """Return the sensor state."""
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs: dict[str, Any] = {
            "relay_entity": self._relay_entity,
            "demand_on": self._demand_on,
            "min_on_duration_min": int(self._min_on_duration / 60),
            "min_off_duration_min": int(self._min_off_duration / 60),
            "in_grace_period": self._state in (STATE_PENDING_ON, STATE_PENDING_OFF),
            "time_remaining_sec": 0,
        }
        if self._demand_entity_id:
            attrs["demand_entity"] = self._demand_entity_id
        if self._last_on:
            attrs["last_on"] = self._last_on.isoformat()
        if self._last_off:
            attrs["last_off"] = self._last_off.isoformat()
        if self._wait_until:
            remaining = (self._wait_until - dt_util.now()).total_seconds()
            attrs["time_remaining_sec"] = max(0, int(remaining))
        return attrs

    async def async_added_to_hass(self):
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        # Restore state from previous state
        if (last_state := await self.async_get_last_state()) is not None:
            valid_states = {STATE_IDLE, STATE_HEATING, STATE_PENDING_ON, STATE_PENDING_OFF}
            if last_state.state in valid_states:
                self._state = last_state.state
            else:
                _LOGGER.warning(
                    "Invalid restored state: %s, resetting to IDLE", last_state.state
                )
                self._state = STATE_IDLE
            
            if "demand_on" in last_state.attributes:
                self._demand_on = bool(last_state.attributes["demand_on"])
            if "last_on" in last_state.attributes:
                self._last_on = dt_util.parse_datetime(
                    last_state.attributes["last_on"]
                )
            if "last_off" in last_state.attributes:
                self._last_off = dt_util.parse_datetime(
                    last_state.attributes["last_off"]
                )
            if "wait_until" in last_state.attributes:
                self._wait_until = dt_util.parse_datetime(
                    last_state.attributes["wait_until"]
                )
                # Recalculate remaining time and restart wait if needed
                if self._wait_until and self._state in (STATE_PENDING_ON, STATE_PENDING_OFF):
                    remaining = (self._wait_until - dt_util.now()).total_seconds()
                    if remaining > 0:
                        self._start_wait(
                            remaining,
                            self._do_turn_on if self._state == STATE_PENDING_ON else self._do_turn_off
                        )

        # Track relay state changes
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._relay_entity], self._on_relay_change
            )
        )

        # Evaluate state after restoration and setup
        await self._evaluate_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when removed."""
        self._cancel_wait()

    async def handle_demand_change(self, demand_on: bool) -> None:
        """Handle a demand change from the internal switch entity."""
        if demand_on == self._demand_on:
            return
        self._demand_on = demand_on
        self._cancel_wait()
        await self._apply_demand_logic()
    async def sync_demand(self, demand_on: bool, demand_entity_id: Optional[str] = None) -> None:
        """Sync demand state from the switch after setup."""
        self._demand_on = demand_on
        if demand_entity_id is not None:
            self._demand_entity_id = demand_entity_id
        await self._evaluate_state()

    def _get_state(self, entity_id: str) -> Optional[str]:
        """Get entity state string safely."""
        state = self.hass.states.get(entity_id)
        return state.state if state else None

    def _is_on(self, entity_id: str) -> bool:
        """Check if entity state is on."""
        return self.hass.states.is_state(entity_id, STATE_ON)

    def _set_state(self, state: str) -> None:
        """Set state and write."""
        self._state = state
        self.async_write_ha_state()

    def _compute_remaining(self, last_time: Optional[datetime], min_duration: int) -> int:
        """Compute remaining wait time in seconds.
        
        If last_time is None, returns full min_duration (forces wait on first use).
        Otherwise, returns max(0, min_duration - elapsed_seconds).
        """
        if last_time is None:
            # No previous timestamp - force full wait for safety on first use
            return min_duration
        elapsed = (dt_util.now() - last_time).total_seconds()
        return max(0, int(min_duration - elapsed))

    def _start_wait(self, duration_sec: int, callback_func: Callable[[], Any]) -> None:
        """Start a wait task and periodic countdown updates."""
        self._cancel_wait()
        if duration_sec <= 0:
            # No need to wait, execute immediately
            self.hass.async_create_task(callback_func())
            return
        self._wait_until = dt_util.now() + timedelta(seconds=duration_sec)
        self.async_write_ha_state()
        self._start_periodic_update()
        self._wait_task = asyncio.create_task(
            self._wait_and_execute(duration_sec, callback_func)
        )

    def _cancel_wait(self) -> None:
        """Cancel any existing wait task."""
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
        self._wait_task = None
        self._wait_until = None
        self._stop_periodic_update()

    def _start_periodic_update(self) -> None:
        """Start periodic state updates for the countdown."""
        self._stop_periodic_update()
        # Update every 5 seconds instead of 1 to reduce database writes
        self._update_unsub = async_track_time_interval(
            self.hass, self._periodic_update, timedelta(seconds=5)
        )

    def _stop_periodic_update(self) -> None:
        """Stop periodic updates."""
        if self._update_unsub and callable(self._update_unsub):
            self._update_unsub()
            self._update_unsub = None

    @callback
    def _periodic_update(self, _now: Optional[datetime] = None) -> None:
        """Refresh state to update the countdown attribute."""
        self.async_write_ha_state()

    async def _wait_and_execute(self, duration_sec: int, callback_func: Callable[[], Any]) -> None:
        """Wait for the specified duration, then execute the callback."""
        try:
            await asyncio.sleep(duration_sec)
            self._wait_task = None
            self._wait_until = None
            self._stop_periodic_update()
            await callback_func()
        except asyncio.CancelledError:
            _LOGGER.debug("Wait task cancelled")
            raise
        except Exception as e:
            _LOGGER.exception(
                "Unexpected error in stove controller wait task: %s", e
            )
            self._wait_task = None
            self._wait_until = None
            self._stop_periodic_update()
            # Re-evaluate state based on current demand
            await self._apply_demand_logic()

    async def _do_turn_on(self) -> None:
        """Turn on the relay if demand is still on."""
        if self._demand_on:
            try:
                await self.hass.services.async_call(
                    "switch",
                    "turn_on",
                    target={"entity_id": self._relay_entity},
                    blocking=True,
                    timeout=10,
                )
                self._set_state(STATE_HEATING)
            except Exception as e:
                _LOGGER.error(
                    "Failed to turn on relay %s: %s", self._relay_entity, e
                )
                # Re-evaluate state - demand is still on, but relay didn't turn on
                await self._apply_demand_logic()
        else:
            self._set_state(STATE_IDLE)

    async def _do_turn_off(self) -> None:
        """Turn off the relay if demand is still off."""
        if not self._demand_on:
            try:
                await self.hass.services.async_call(
                    "switch",
                    "turn_off",
                    target={"entity_id": self._relay_entity},
                    blocking=True,
                    timeout=10,
                )
                self._set_state(STATE_IDLE)
            except Exception as e:
                _LOGGER.error(
                    "Failed to turn off relay %s: %s", self._relay_entity, e
                )
                # Re-evaluate state - demand is still off, but relay didn't turn off
                await self._apply_demand_logic()
        else:
            self._set_state(STATE_HEATING)

    async def _apply_demand_logic(self) -> None:
        """Apply demand change logic based on current state.
        
        This is the core state transition logic that determines what to do
        based on the current demand and relay state.
        """
        relay_on = self._is_on(self._relay_entity)
        
        if self._demand_on and not relay_on:
            # Demand is ON but relay is OFF - need to turn relay on
            remaining = self._compute_remaining(
                self._last_off, self._min_off_duration
            )
            if remaining > 0:
                self._set_state(STATE_PENDING_ON)
                self._start_wait(remaining, self._do_turn_on)
            else:
                await self._do_turn_on()
        elif not self._demand_on and relay_on:
            # Demand is OFF but relay is ON - need to turn relay off
            remaining = self._compute_remaining(
                self._last_on, self._min_on_duration
            )
            if remaining > 0:
                self._set_state(STATE_PENDING_OFF)
                self._start_wait(remaining, self._do_turn_off)
            else:
                await self._do_turn_off()
        elif self._demand_on and relay_on:
            # Demand is ON and relay is ON - heating state
            self._set_state(STATE_HEATING)
        else:
            # Demand is OFF and relay is OFF - idle state
            self._set_state(STATE_IDLE)

    async def _evaluate_state(self) -> None:
        """Evaluate and set the correct state based on current conditions."""
        relay_state = self._get_state(self._relay_entity)

        if relay_state is None:
            _LOGGER.warning(
                "Relay entity %s not available yet",
                self._relay_entity,
            )
            self._set_state(STATE_IDLE)
            return

        self._cancel_wait()
        await self._apply_demand_logic()

    async def _on_relay_change(self, event: Any) -> None:
        """Handle relay state change - update timestamps and re-evaluate."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        
        # Skip if no state or state hasn't actually changed
        if new_state is None or (old_state and old_state.state == new_state.state):
            return
        
        if new_state.state == STATE_ON:
            self._last_on = dt_util.now()
            self._last_off = None
        elif new_state.state == STATE_OFF:
            self._last_off = dt_util.now()
            self._last_on = None
        else:
            _LOGGER.warning("Unknown relay state: %s", new_state.state)
            return
        
        self.async_write_ha_state()
        # Re-evaluate demand state after external relay change
        await self._apply_demand_logic()
