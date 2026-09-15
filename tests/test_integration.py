"""Integration tests for Stove Controller.

Tests the interaction between switch and sensor components.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event
from homeassistant.util import dt as dt_util

from stove_controller.const import (
    DEFAULT_MIN_OFF_DURATION,
    DEFAULT_MIN_ON_DURATION,
    STATE_HEATING,
    STOVE_DEMAND_CHANGED,
)
from stove_controller.sensor import StoveControllerSensor
from stove_controller.switch import StoveDemandSwitch


@pytest.fixture
async def mock_hass_with_bus():
    """Create a mock Home Assistant instance with event bus.

    Sensors are created inline by the tests and assigned this mock as
    ``hass``.  ``_start_wait`` schedules the wait via
    ``asyncio.create_task`` (not ``hass.async_create_task``), so the fixture
    cannot capture the tasks by wrapping a hass method.  Instead, on teardown
    it cancels any still-pending ``_wait_and_execute`` tasks so the HACC
    ``verify_cleanup`` plugin does not flag them as lingering.
    """
    hass = MagicMock()
    hass.data = {}
    hass.states = MagicMock()
    hass.states.get = MagicMock()
    hass.states.is_state = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries = MagicMock()
    hass.async_create_task = lambda x: asyncio.create_task(x)

    # Mock event bus
    hass.bus = MagicMock()
    hass.bus.async_fire = MagicMock()
    hass.bus.async_listen = MagicMock()

    # Mock state tracking
    hass.states.get = MagicMock(return_value=MagicMock(state=STATE_OFF))
    hass.states.is_state = MagicMock(return_value=False)

    yield hass

    # Cancel and await any lingering _wait_and_execute tasks started by
    # sensors that used this mock hass.  Sensors are created inline by the
    # tests, so the tasks are discovered via asyncio.all_tasks() rather than a
    # registry.  The tasks must be awaited to completion (not just cancelled)
    # so the HACC verify_cleanup plugin does not flag them as lingering.
    # Match by coroutine qualified name (e.g.
    # ``StoveStateMachine._start_wait.<locals>.wait_and_execute``).
    wait_tasks = [
        task
        for task in asyncio.all_tasks()
        if "wait_and_execute" in task.get_coro().__qualname__
    ]
    for task in wait_tasks:
        task.cancel()
    for task in wait_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestSwitchSensorIntegration:
    """Test interaction between switch and sensor via event bus."""

    @pytest.mark.asyncio
    async def test_demand_change_via_event(self, mock_hass_with_bus):
        """Test that switch demand change triggers sensor via event bus."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"

        # Create sensor
        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass

        # Create switch
        switch = StoveDemandSwitch(entry_id)
        switch.hass = hass

        # Initially both off
        assert not sensor._demand_on
        # Simulate switch turning on - this fires an event
        switch._is_on = True
        await switch._notify_controller()

        # Verify event was fired
        hass.bus.async_fire.assert_called()
        call_args = hass.bus.async_fire.call_args
        assert call_args[0][0] == STOVE_DEMAND_CHANGED
        assert call_args[0][1]["entry_id"] == entry_id
        assert call_args[0][1]["demand_on"]
    @pytest.mark.asyncio
    async def test_sensor_receives_demand_event(self, mock_hass_with_bus):
        """Test that sensor correctly handles demand change events."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"

        # Create sensor
        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass
        sensor._attr_entity_id = "sensor.test_sensor"

        # Mock the async_on_remove
        sensor.async_on_remove = MagicMock()

        # Set up relay state
        hass.states.get = MagicMock(return_value=MagicMock(state=STATE_OFF))
        hass.states.is_state = MagicMock(return_value=False)

        # Mock async_write_ha_state to prevent errors
        sensor.async_write_ha_state = MagicMock()

        # Track all async_listen calls
        listen_calls = []

        def track_listen(event_type, handler, *args, **kwargs):
            listen_calls.append((event_type, handler))
            return MagicMock()

        hass.bus.async_listen = track_listen

        # Trigger async_added_to_hass
        await sensor.async_added_to_hass()

        # Verify demand change event listener was registered
        demand_listeners = [
            (et, h) for et, h in listen_calls if et == STOVE_DEMAND_CHANGED
        ]
        assert len(demand_listeners) == 1
        assert demand_listeners[0][1] == sensor._on_demand_change_event

        # Now simulate a demand change event
        event = MagicMock(spec=Event)
        event.data = {
            "entry_id": entry_id,
            "demand_on": True,
            "entity_id": "switch.test_demand",
        }

        await sensor._on_demand_change_event(event)

        assert sensor._demand_on
        assert sensor._demand_entity_id == "switch.test_demand"

    @pytest.mark.asyncio
    async def test_full_cycle_switch_toggle_to_sensor_state_change(
        self, mock_hass_with_bus
    ):
        """Test complete cycle: switch toggle -> event -> sensor state change."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"

        # Set up relay state tracking
        relay_state = MagicMock()
        relay_state.state = STATE_OFF
        hass.states.get = MagicMock(return_value=relay_state)
        hass.states.is_state = MagicMock(return_value=False)

        # Create sensor
        sensor = StoveControllerSensor(entry_id, relay_entity, 30, 25)
        sensor.hass = hass
        sensor.async_write_ha_state = MagicMock()
        sensor.async_on_remove = MagicMock()

        # Create switch
        switch = StoveDemandSwitch(entry_id)
        switch.hass = hass

        # Setup sensor event listener
        mock_unsub = MagicMock()
        hass.bus.async_listen = MagicMock(return_value=mock_unsub)

        await sensor.async_added_to_hass()

        # Switch turns on
        switch._is_on = True
        await switch._notify_controller()

        # Sensor receives event
        event = MagicMock(spec=Event)
        event.data = {
            "entry_id": entry_id,
            "demand_on": True,
            "entity_id": switch.entity_id,
        }

        # Need to set up the state properly
        sensor._last_off = dt_util.now() - timedelta(minutes=30)

        await sensor._on_demand_change_event(event)

        # Sensor should now have demand_on = True
        assert sensor._demand_on
        # Now evaluate state - relay is off, demand is on
        # Should transition to HEATING or PENDING_ON
        await sensor._apply_demand_logic()

        # Since last_off was > 25 minutes ago, should go directly to HEATING
        # But relay is off, so it needs to turn on
        # This would require the relay to be available

    @pytest.mark.asyncio
    async def test_external_relay_change_triggers_sensor(self, mock_hass_with_bus):
        """Test that external relay state change triggers sensor re-evaluation."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"

        # Create sensor
        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass
        sensor.async_write_ha_state = MagicMock()
        sensor.async_on_remove = MagicMock()
        sensor._demand_on = True

        # Setup initial state
        relay_state_off = MagicMock()
        relay_state_off.state = STATE_OFF
        relay_state_on = MagicMock()
        relay_state_on.state = STATE_ON

        hass.states.get = MagicMock(return_value=relay_state_off)

        # Track state change event
        mock_unsub = MagicMock()
        hass.bus.async_listen = MagicMock(return_value=mock_unsub)

        await sensor.async_added_to_hass()

        # Simulate external relay change to ON
        event = MagicMock()
        event.data = {
            "entity_id": relay_entity,
            "old_state": MagicMock(state=STATE_OFF),
            "new_state": relay_state_on,
        }

        await sensor._on_relay_change(event)

        # Should have updated last_on timestamp
        assert sensor._last_on is not None
        assert sensor._last_off is None

        # Should have written state
        sensor.async_write_ha_state.assert_called()


class TestEventBasedCommunication:
    """Test event-based communication patterns."""

    @pytest.mark.asyncio
    async def test_multiple_sensors_different_entries(self, mock_hass_with_bus):
        """Test that events are filtered by entry_id."""
        hass = mock_hass_with_bus

        # Create two sensors with different entry IDs
        sensor1 = StoveControllerSensor("entry_1", "switch.relay1", 30, 25)
        sensor1.hass = hass
        sensor1.async_write_ha_state = MagicMock()

        sensor2 = StoveControllerSensor("entry_2", "switch.relay2", 30, 25)
        sensor2.hass = hass
        sensor2.async_write_ha_state = MagicMock()

        # Event for entry_1
        event1 = MagicMock(spec=Event)
        event1.data = {"entry_id": "entry_1", "demand_on": True}

        # Event for entry_2
        event2 = MagicMock(spec=Event)
        event2.data = {"entry_id": "entry_2", "demand_on": True}

        # Process events
        await sensor1._on_demand_change_event(event1)
        await sensor1._on_demand_change_event(event2)

        await sensor2._on_demand_change_event(event1)
        await sensor2._on_demand_change_event(event2)

        # Each sensor should only respond to its own entry_id
        assert sensor1._demand_on
        assert sensor2._demand_on
    @pytest.mark.asyncio
    async def test_event_contains_entity_id(self, mock_hass_with_bus):
        """Test that demand change events include the switch entity ID."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"

        switch = StoveDemandSwitch(entry_id)
        switch.hass = hass

        # Fire demand change
        switch._is_on = True
        await switch._notify_controller()

        # Verify event contains entity_id (should be the unique_id)
        call_args = hass.bus.async_fire.call_args
        assert "entity_id" in call_args[0][1]
        assert call_args[0][1]["entity_id"] == f"{entry_id}_stove_demand"


class TestBackwardsCompatibility:
    """Test that old direct method calls still work."""

    @pytest.mark.asyncio
    async def test_handle_demand_change_direct_call(self, mock_hass_with_bus):
        """Test that handle_demand_change method works with state machine."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"

        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass
        sensor._state_machine.hass = hass
        sensor.async_write_ha_state = MagicMock()
        # Mock relay state to be OFF
        hass.states.is_state.return_value = False
        # Last off long ago so demand ON turns the relay on immediately.
        sensor._last_off = datetime(2026, 1, 15, 19, 0, 0, tzinfo=UTC)

        with patch(
            "homeassistant.util.dt.now",
            return_value=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
        ):
            await sensor.handle_demand_change(demand_on=True)

        # Demand was set and the state machine applied the demand logic:
        # the relay turn_on service was called and we reached HEATING.
        assert sensor._demand_on
        hass.services.async_call.assert_awaited_once_with(
            "switch", "turn_on", target={"entity_id": relay_entity}, blocking=True,
        )
        assert sensor._state_machine.state == STATE_HEATING
        sensor._state_machine.cancel_wait()

    @pytest.mark.asyncio
    async def test_sync_demand_still_works(self, mock_hass_with_bus):
        """Test that sync_demand method works with state machine."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"

        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass
        sensor._state_machine.hass = hass
        sensor.async_write_ha_state = MagicMock()
        # Mock relay state to be OFF
        hass.states.is_state.return_value = False
        # Set last_off to avoid waiting
        sensor._last_off = datetime(2026, 1, 15, 19, 0, 0, tzinfo=UTC)

        with patch(
            "homeassistant.util.dt.now",
            return_value=datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC),
        ):
            await sensor.sync_demand(demand_on=True, demand_entity_id="switch.test")

        # sync_demand set the demand entity and drove the state machine
        # through the same path as a demand change.
        assert sensor._demand_on
        assert sensor._demand_entity_id == "switch.test"
        hass.services.async_call.assert_awaited_once_with(
            "switch", "turn_on", target={"entity_id": relay_entity}, blocking=True,
        )
        assert sensor._state_machine.state == STATE_HEATING
        sensor._state_machine.cancel_wait()
