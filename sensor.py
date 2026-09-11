"""Sensor platform for the Stove Controller integration."""

import asyncio
import logging
from datetime import timedelta

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
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the Stove Controller sensor."""
    data = {**entry.data, **entry.options}
    relay_entity = data[CONF_RELAY_ENTITY]
    min_on_min = data.get(CONF_MIN_ON_DURATION, DEFAULT_MIN_ON_DURATION)
    min_off_min = data.get(CONF_MIN_OFF_DURATION, DEFAULT_MIN_OFF_DURATION)

    sensor = StoveControllerSensor(
        entry.entry_id, relay_entity, min_on_min, min_off_min
    )
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["sensor"] = sensor
    async_add_entities([sensor])


class StoveControllerSensor(RestoreEntity, SensorEntity):
    """Sensor that controls the stove relay with anti-short-cycle logic."""

    def __init__(
        self, entry_id, relay_entity, min_on_min, min_off_min
    ) -> None:
        """Initialize the sensor."""
        self._entry_id = entry_id
        self._relay_entity = relay_entity
        self._min_on_duration = min_on_min * 60  # seconds
        self._min_off_duration = min_off_min * 60  # seconds

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

        self._state = STATE_IDLE
        self._demand_on = False
        self._demand_entity_id = None
        self._last_on = None
        self._last_off = None
        self._wait_until = None
        self._wait_task = None
        self._update_unsub = None

    @property
    def native_value(self):
        """Return the sensor state."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        attrs = {
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

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in (
                STATE_IDLE,
                STATE_HEATING,
                STATE_PENDING_ON,
                STATE_PENDING_OFF,
            ):
                self._state = last_state.state
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

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._relay_entity], self._on_relay_change
            )
        )

        await self._evaluate_state()

    async def async_will_remove_from_hass(self):
        """Clean up when removed."""
        self._cancel_wait()

    async def handle_demand_change(self, demand_on: bool):
        """Handle a demand change from the internal switch entity."""
        if demand_on == self._demand_on:
            return
        self._demand_on = demand_on
        self._cancel_wait()

        relay_on = self._is_on(self._relay_entity)

        if demand_on and not relay_on:
            remaining = self._compute_remaining(
                self._last_off, self._min_off_duration
            )
            if remaining > 0:
                self._set_state(STATE_PENDING_ON)
                self._start_wait(remaining, self._do_turn_on)
            else:
                await self._do_turn_on()
        elif not demand_on and relay_on:
            remaining = self._compute_remaining(
                self._last_on, self._min_on_duration
            )
            if remaining > 0:
                self._set_state(STATE_PENDING_OFF)
                self._start_wait(remaining, self._do_turn_off)
            else:
                await self._do_turn_off()
        elif demand_on and relay_on:
            self._set_state(STATE_HEATING)
        else:
            self._set_state(STATE_IDLE)

    async def sync_demand(self, demand_on: bool, demand_entity_id: str = None):
        """Sync demand state from the switch after setup."""
        self._demand_on = demand_on
        if demand_entity_id is not None:
            self._demand_entity_id = demand_entity_id
        await self._evaluate_state()

    def _get_state(self, entity_id):
        """Get entity state string safely."""
        state = self.hass.states.get(entity_id)
        return state.state if state else None

    def _is_on(self, entity_id):
        """Check if entity state is on."""
        return self.hass.states.is_state(entity_id, STATE_ON)

    def _set_state(self, state):
        """Set state and write."""
        self._state = state
        self.async_write_ha_state()

    def _compute_remaining(self, last_time, min_duration):
        """Compute remaining wait time in seconds."""
        if last_time is None:
            return min_duration
        elapsed = (dt_util.now() - last_time).total_seconds()
        return max(0, min_duration - elapsed)

    def _start_wait(self, duration_sec, callback_func):
        """Start a wait task and periodic countdown updates."""
        self._cancel_wait()
        self._wait_until = dt_util.now() + timedelta(seconds=duration_sec)
        self.async_write_ha_state()
        self._start_periodic_update()
        self._wait_task = asyncio.create_task(
            self._wait_and_execute(duration_sec, callback_func)
        )

    def _cancel_wait(self):
        """Cancel any existing wait task."""
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
        self._wait_task = None
        self._wait_until = None
        self._stop_periodic_update()

    def _start_periodic_update(self):
        """Start 1-second periodic state updates for the countdown."""
        self._stop_periodic_update()
        self._update_unsub = async_track_time_interval(
            self.hass, self._periodic_update, timedelta(seconds=1)
        )

    def _stop_periodic_update(self):
        """Stop periodic updates."""
        if self._update_unsub:
            self._update_unsub()
            self._update_unsub = None

    @callback
    def _periodic_update(self, _now=None):
        """Refresh state to update the countdown attribute."""
        self.async_write_ha_state()

    async def _wait_and_execute(self, duration_sec, callback_func):
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
        except Exception:
            _LOGGER.exception(
                "Unexpected error in stove controller wait task"
            )
            self._wait_task = None
            self._wait_until = None
            self._stop_periodic_update()
            self._set_state(STATE_IDLE)

    async def _do_turn_on(self):
        """Turn on the relay if demand is still on."""
        if self._demand_on:
            await self.hass.services.async_call(
                "switch",
                "turn_on",
                target={"entity_id": self._relay_entity},
            )
            self._set_state(STATE_HEATING)
        else:
            self._set_state(STATE_IDLE)

    async def _do_turn_off(self):
        """Turn off the relay if demand is still off."""
        if not self._demand_on:
            await self.hass.services.async_call(
                "switch",
                "turn_off",
                target={"entity_id": self._relay_entity},
            )
            self._set_state(STATE_IDLE)
        else:
            self._set_state(STATE_HEATING)

    async def _evaluate_state(self):
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

        relay_on = relay_state == STATE_ON

        if self._demand_on and relay_on:
            self._set_state(STATE_HEATING)
        elif not self._demand_on and not relay_on:
            self._set_state(STATE_IDLE)
        elif self._demand_on and not relay_on:
            remaining = self._compute_remaining(
                self._last_off, self._min_off_duration
            )
            if remaining > 0:
                self._set_state(STATE_PENDING_ON)
                self._start_wait(remaining, self._do_turn_on)
            else:
                await self._do_turn_on()
        elif not self._demand_on and relay_on:
            remaining = self._compute_remaining(
                self._last_on, self._min_on_duration
            )
            if remaining > 0:
                self._set_state(STATE_PENDING_OFF)
                self._start_wait(remaining, self._do_turn_off)
            else:
                await self._do_turn_off()

    async def _on_relay_change(self, event):
        """Handle relay state change - update timestamps."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        if new_state.state == STATE_ON:
            self._last_on = dt_util.now()
        elif new_state.state == STATE_OFF:
            self._last_off = dt_util.now()

        self.async_write_ha_state()
