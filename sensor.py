"""Sensor platform for the Stove Controller integration."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, UnitOfTime
from homeassistant.core import Event, HomeAssistant, callback
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
    CONF_UPDATE_INTERVAL,
    DEFAULT_MIN_OFF_DURATION,
    DEFAULT_MIN_ON_DURATION,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    STOVE_DEMAND_CHANGED,
)
from .state_machine import StoveStateMachine

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> None:
    """Set up the Stove Controller sensor."""
    data = {**entry.data, **entry.options}
    relay_entity = data[CONF_RELAY_ENTITY]
    min_on_min = data.get(CONF_MIN_ON_DURATION, DEFAULT_MIN_ON_DURATION)
    min_off_min = data.get(CONF_MIN_OFF_DURATION, DEFAULT_MIN_OFF_DURATION)
    update_interval = data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

    sensor = StoveControllerSensor(
        entry.entry_id, relay_entity, min_on_min, min_off_min, update_interval
    )

    # Create subordinate sensors that surface on the device page
    remaining_sensor = StoveRemainingTimeSensor(entry.entry_id, sensor)
    last_on_sensor = StoveLastOnSensor(entry.entry_id, sensor)
    last_off_sensor = StoveLastOffSensor(entry.entry_id, sensor)

    sensor._sub_sensors = [remaining_sensor, last_on_sensor, last_off_sensor]

    # Store reference for backwards compatibility with __init__.py sync
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    store["sensor"] = sensor
    async_add_entities([sensor, remaining_sensor, last_on_sensor, last_off_sensor])


class StoveControllerSensor(RestoreEntity, SensorEntity):
    """Sensor that controls the stove relay with anti-short-cycle logic.

    Uses composition with StoveStateMachine for explicit state management.
    """

    def __init__(
        self,
        entry_id: str,
        relay_entity: str,
        min_on_min: int,
        min_off_min: int,
        update_interval: int = DEFAULT_UPDATE_INTERVAL,
    ) -> None:
        """Initialize the sensor."""
        self._entry_id: str = entry_id
        self._relay_entity: str = relay_entity
        self._min_on_duration: int = min_on_min * 60
        self._min_off_duration: int = min_off_min * 60
        self._update_interval: int = update_interval

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

        # Initialize state machine
        self._state_machine = StoveStateMachine(
            hass=None,
            relay_entity=relay_entity,
            min_on_duration=self._min_on_duration,
            min_off_duration=self._min_off_duration,
        )

        self._sub_sensors: list[SensorEntity] = []
        self._update_unsub: Callable | None = None
        self._demand_entity_id: str | None = None

    @property
    def _state(self) -> str:
        """Delegate to state machine state."""
        return str(self._state_machine.state)

    @_state.setter
    def _state(self, value: str) -> None:
        """Set state machine state - uses proper transition API."""
        from .const import ControllerState
        state = ControllerState(value)
        # Use async run to call transition_to from sync context if needed
        # For tests, directly set the state
        self._state_machine._state = state

    @property
    def _demand_on(self) -> bool:
        """Delegate to state machine demand_on."""
        return self._state_machine.demand_on

    @_demand_on.setter
    def _demand_on(self, value: bool) -> None:
        """Set state machine demand_on."""
        self._state_machine._demand_on = value

    @property
    def _last_on(self) -> datetime | None:
        """Delegate to state machine last_on."""
        return self._state_machine.last_on

    @_last_on.setter
    def _last_on(self, value: datetime | None) -> None:
        """Set state machine last_on."""
        self._state_machine._last_on = value

    @property
    def _last_off(self) -> datetime | None:
        """Delegate to state machine last_off."""
        return self._state_machine.last_off

    @_last_off.setter
    def _last_off(self, value: datetime | None) -> None:
        """Set state machine last_off."""
        self._state_machine._last_off = value

    @property
    def _wait_until(self) -> datetime | None:
        """Delegate to state machine wait_until."""
        return self._state_machine.wait_until

    @_wait_until.setter
    def _wait_until(self, value: datetime | None) -> None:
        """Set state machine wait_until."""
        self._state_machine._wait_until = value

    @property
    def _wait_task(self) -> asyncio.Task | None:
        """Delegate to state machine wait_task."""
        return self._state_machine.wait_task

    @_wait_task.setter
    def _wait_task(self, value: asyncio.Task | None) -> None:
        """Set state machine wait_task."""
        self._state_machine._wait_task = value

    @property
    def native_value(self) -> str:
        """Return the sensor state."""
        return str(self._state_machine.state)

    def _get_state(self, entity_id: str) -> Any:
        """Get entity state - delegate to HA."""
        if self.hass is None:
            return None
        return self.hass.states.get(entity_id)

    async def _evaluate_state(self) -> None:
        """Delegate to state machine public API."""
        await self._state_machine.evaluate()

    def _compute_remaining(
        self, last_time: datetime | None, min_duration: int
    ) -> int:
        """Delegate to state machine public API."""
        return self._state_machine.compute_remaining(last_time, min_duration)

    async def _apply_demand_logic(self) -> None:
        """Delegate to state machine internal method."""
        await self._state_machine._apply_demand_logic()

    def _start_periodic_update(self) -> None:
        """Start periodic state updates for the countdown."""
        self._stop_periodic_update()
        # Republish state at the configured interval so the time_remaining_sec
        # attribute counts down while in a grace period.  This mirrors the
        # pre-refactor behaviour where _start_wait registered a time tracker;
        # the e2e tests advance time via async_fire_time_changed, which fires
        # this tracker so the countdown is observable.
        self._update_unsub = async_track_time_interval(
            self.hass, self._periodic_update, timedelta(seconds=self._update_interval)
        )

    def _stop_periodic_update(self) -> None:
        """Stop periodic state updates."""
        if self._update_unsub and callable(self._update_unsub):
            self._update_unsub()
            self._update_unsub = None

    @callback
    def _periodic_update(self, _now: datetime | None = None) -> None:
        """Refresh state to update the countdown attribute."""
        self.async_write_ha_state()
        self._update_sub_sensors()
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs: dict[str, Any] = {
            "relay_entity": self._relay_entity,
            "demand_on": self._state_machine.demand_on,
            "min_on_duration_min": int(self._min_on_duration / 60),
            "min_off_duration_min": int(self._min_off_duration / 60),
            "update_interval_sec": self._update_interval,
            "in_grace_period": self._state_machine.is_in_grace_period,
            "time_remaining_sec": self._state_machine.get_remaining_seconds(),
        }
        if self._demand_entity_id:
            attrs["demand_entity"] = self._demand_entity_id
        if self._state_machine.last_on:
            attrs["last_on"] = self._state_machine.last_on.isoformat()
        if self._state_machine.last_off:
            attrs["last_off"] = self._state_machine.last_off.isoformat()
        return attrs

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()

        self._state_machine.hass = self.hass
        self._state_machine.on_state_change = self._on_state_change

        # Restore state
        if (last_state := await self.async_get_last_state()) is not None:
            self._state_machine.restore_state(
                state=last_state.state,
                demand_on=bool(last_state.attributes.get("demand_on", False)),
                last_on=dt_util.parse_datetime(last_state.attributes["last_on"])
                    if "last_on" in last_state.attributes else None,
                last_off=dt_util.parse_datetime(last_state.attributes["last_off"])
                    if "last_off" in last_state.attributes else None,
                wait_until=dt_util.parse_datetime(last_state.attributes["wait_until"])
                    if "wait_until" in last_state.attributes else None,
            )

        self.async_on_remove(
            self.hass.bus.async_listen(
                STOVE_DEMAND_CHANGED, self._on_demand_change_event
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._relay_entity], self._on_relay_change
            )
        )

        await self._state_machine.evaluate()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when removed."""
        self._state_machine.cancel_wait()
        if self._update_unsub and callable(self._update_unsub):
            self._update_unsub()
        self._update_unsub = None

    async def _on_state_change(self, state: str) -> None:
        """Callback for state machine state changes."""
        self.async_write_ha_state()
        self._update_sub_sensors()
        # Start/stop the countdown refresh depending on whether a wait is
        # now active (grace period).  Starting here also covers the case where
        # _start_wait set _wait_until after the PENDING_* transition already
        # published state with time_remaining_sec == 0.
        if self._state_machine.is_in_grace_period and self._state_machine.wait_until:
            self._start_periodic_update()
        else:
            self._stop_periodic_update()

    def _update_sub_sensors(self) -> None:
        """Push state updates to subordinate sensor entities."""
        for sub in self._sub_sensors:
            if getattr(sub, "entity_id", None) is not None:
                sub.async_write_ha_state()

    async def handle_demand_change(self, demand_on: bool) -> None:
        """Handle a demand change from the internal switch entity."""
        relay_on = (
            self.hass.states.is_state(self._relay_entity, STATE_ON)
            if self.hass
            else False
        )
        await self._state_machine.set_demand(demand_on, relay_on)

    async def sync_demand(
        self, demand_on: bool, demand_entity_id: str | None = None
    ) -> None:
        """Sync demand state from the switch."""
        if demand_entity_id is not None:
            self._demand_entity_id = demand_entity_id
        await self._state_machine.set_demand(demand_on)

    @callback
    async def _on_demand_change_event(self, event: Event) -> None:
        """Handle demand change events."""
        if event.data.get("entry_id") != self._entry_id:
            return
        demand_on = event.data.get("demand_on")
        demand_entity_id = event.data.get("entity_id")
        if demand_entity_id is not None:
            self._demand_entity_id = demand_entity_id
        await self._state_machine.set_demand(bool(demand_on))

    @callback
    async def _on_relay_change(self, event: Any) -> None:
        """Handle relay state change."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        
        # Skip if no new state
        if new_state is None:
            return
        
        # Handle relay appearance event (old_state=None, e.g., HA restart)
        # Preserve timestamps but re-evaluate demand
        if old_state is None:
            await self._state_machine.evaluate()
            return
        
        # Skip if state hasn't actually changed
        if old_state.state == new_state.state:
            return
        
        # Extract state string
        new_state_str = new_state.state if hasattr(new_state, 'state') else new_state
        
        await self._state_machine.update_relay_state(new_state_str)

    async def async_check_health(self) -> None:
        """Check if relay entity is available."""
        relay_state = self.hass.states.get(self._relay_entity)
        if relay_state is None:
            raise Exception(f"Relay entity {self._relay_entity} not available")


class StoveRemainingTimeSensor(SensorEntity):
    """Sensor showing the remaining anti-short-cycle wait time in seconds."""

    def __init__(self, entry_id: str, controller: StoveControllerSensor) -> None:
        self._entry_id = entry_id
        self._controller = controller
        self._attr_name = "Stove Remaining Time"
        self._attr_unique_id = f"{entry_id}_remaining_time"
        self._attr_icon = "mdi:timer-sand"
        self._attr_should_poll = False
        self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Stove Controller",
            manufacturer="Custom",
            model="A251 Controller",
        )

    @property
    def native_value(self) -> int:
        return self._controller._state_machine.get_remaining_seconds()


class StoveLastOnSensor(SensorEntity):
    """Sensor showing when the stove relay was last turned on."""

    def __init__(self, entry_id: str, controller: StoveControllerSensor) -> None:
        self._entry_id = entry_id
        self._controller = controller
        self._attr_name = "Stove Last On"
        self._attr_unique_id = f"{entry_id}_last_on"
        self._attr_icon = "mdi:clock-start"
        self._attr_should_poll = False
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Stove Controller",
            manufacturer="Custom",
            model="A251 Controller",
        )

    @property
    def native_value(self) -> datetime | None:
        return self._controller._state_machine.last_on


class StoveLastOffSensor(SensorEntity):
    """Sensor showing when the stove relay was last turned off."""

    def __init__(self, entry_id: str, controller: StoveControllerSensor) -> None:
        self._entry_id = entry_id
        self._controller = controller
        self._attr_name = "Stove Last Off"
        self._attr_unique_id = f"{entry_id}_last_off"
        self._attr_icon = "mdi:clock-end"
        self._attr_should_poll = False
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Stove Controller",
            manufacturer="Custom",
            model="A251 Controller",
        )

    @property
    def native_value(self) -> datetime | None:
        return self._controller._state_machine.last_off
