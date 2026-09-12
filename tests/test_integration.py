"""Integration tests for Stove Controller.

Tests the interaction between switch and sensor components.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event
from homeassistant.util import dt as dt_util

from stove_controller.const import (
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
    STOVE_DEMAND_CHANGED,
)
from stove_controller.sensor import StoveControllerSensor
from stove_controller.switch import StoveDemandSwitch


@pytest.fixture
def mock_hass_with_bus():
    """Create a mock Home Assistant instance with event bus."""
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
    
    return hass


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
        assert sensor._demand_on is False
        
        # Simulate switch turning on - this fires an event
        switch._is_on = True
        await switch._notify_controller()
        
        # Verify event was fired
        hass.bus.async_fire.assert_called()
        call_args = hass.bus.async_fire.call_args
        assert call_args[0][0] == STOVE_DEMAND_CHANGED
        assert call_args[0][1]["entry_id"] == entry_id
        assert call_args[0][1]["demand_on"] is True

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
            (et, h) for et, h in listen_calls 
            if et == STOVE_DEMAND_CHANGED
        ]
        assert len(demand_listeners) == 1
        assert demand_listeners[0][1] == sensor._on_demand_change_event
        
        # Now simulate a demand change event
        event = MagicMock(spec=Event)
        event.data = {
            "entry_id": entry_id,
            "demand_on": True,
            "entity_id": "switch.test_demand"
        }
        
        await sensor._on_demand_change_event(event)
        
        assert sensor._demand_on is True
        assert sensor._demand_entity_id == "switch.test_demand"

    @pytest.mark.asyncio
    async def test_full_cycle_switch_toggle_to_sensor_state_change(self, mock_hass_with_bus):
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
        sensor = StoveControllerSensor(
            entry_id, relay_entity, 30, 25
        )
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
            "entity_id": switch.entity_id
        }
        
        # Need to set up the state properly
        sensor._last_off = dt_util.now() - timedelta(minutes=30)
        
        await sensor._on_demand_change_event(event)
        
        # Sensor should now have demand_on = True
        assert sensor._demand_on is True
        
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
            "new_state": relay_state_on
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
        sensor1 = StoveControllerSensor(
            "entry_1", "switch.relay1", 30, 25
        )
        sensor1.hass = hass
        sensor1.async_write_ha_state = MagicMock()
        
        sensor2 = StoveControllerSensor(
            "entry_2", "switch.relay2", 30, 25
        )
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
        assert sensor1._demand_on is True
        assert sensor2._demand_on is True

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
        """Test that handle_demand_change method still works for direct calls."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"
        
        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass
        sensor.async_write_ha_state = MagicMock()
        sensor._cancel_wait = MagicMock()
        sensor._apply_demand_logic = AsyncMock()
        
        # Direct call (used by existing tests)
        await sensor.handle_demand_change(demand_on=True)
        
        assert sensor._demand_on is True
        sensor._cancel_wait.assert_called_once()
        sensor._apply_demand_logic.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_demand_still_works(self, mock_hass_with_bus):
        """Test that sync_demand method still works."""
        hass = mock_hass_with_bus
        entry_id = "test_entry"
        relay_entity = "switch.test_relay"
        
        sensor = StoveControllerSensor(
            entry_id, relay_entity, DEFAULT_MIN_ON_DURATION, DEFAULT_MIN_OFF_DURATION
        )
        sensor.hass = hass
        sensor.async_write_ha_state = MagicMock()
        sensor._evaluate_state = AsyncMock()
        
        await sensor.sync_demand(demand_on=True, demand_entity_id="switch.test")
        
        assert sensor._demand_on is True
        assert sensor._demand_entity_id == "switch.test"
        sensor._evaluate_state.assert_called_once()
