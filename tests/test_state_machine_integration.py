"""Integration tests for StoveStateMachine in StoveControllerSensor."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)
from stove_controller.sensor import StoveControllerSensor


def _now() -> datetime:
    """Fixed timestamp for consistent testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


@pytest.fixture
def setup_sensor_with_hass():
    """Set up a sensor with a mock hass."""
    sensor = StoveControllerSensor(
        entry_id="test_entry_id",
        relay_entity="switch.test_relay",
        min_on_min=30,
        min_off_min=25,
    )

    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock()
    hass.states.is_state = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.data = {}
    hass.bus = MagicMock()
    hass.bus.async_listen = MagicMock(return_value=MagicMock())
    hass.bus.async_fire = MagicMock()

    sensor.hass = hass
    sensor.async_on_remove = MagicMock()
    sensor.async_write_ha_state = MagicMock()
    sensor.async_get_last_state = AsyncMock(return_value=None)
    sensor._sub_sensors = []

    return sensor, hass


class TestStateMachineIntegration:
    """Test that the sensor properly uses the state machine."""

    @pytest.mark.asyncio
    async def test_state_machine_initialized(self, setup_sensor_with_hass):
        """Test that state machine is initialized in async_added_to_hass."""
        sensor, hass = setup_sensor_with_hass

        await sensor.async_added_to_hass()

        assert sensor._state_machine is not None
        assert sensor._state_machine.relay_entity == "switch.test_relay"
        assert sensor._state_machine.min_on_duration == 30 * 60
        assert sensor._state_machine.min_off_duration == 25 * 60

    @pytest.mark.asyncio
    async def test_state_machine_state_sync(self, setup_sensor_with_hass):
        """Test that sensor state stays in sync with state machine."""
        sensor, hass = setup_sensor_with_hass
        hass.states.is_state.return_value = False  # relay OFF

        await sensor.async_added_to_hass()

        # Sensor native value mirrors the state machine state.
        assert sensor.native_value == STATE_IDLE
        assert sensor._state_machine.state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_handle_demand_change_uses_state_machine(
        self, setup_sensor_with_hass
    ):
        """Test that handle_demand_change delegates to state machine."""
        sensor, hass = setup_sensor_with_hass
        hass.states.is_state.return_value = False  # relay OFF

        await sensor.async_added_to_hass()

        # Set up: last off was long ago, so no wait needed
        sensor._last_off = _now() - timedelta(minutes=30)
        sensor._state_machine._last_off = _now() - timedelta(minutes=30)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        # State machine should have processed the demand
        assert sensor._state_machine.demand_on

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "target",
        [STATE_HEATING, STATE_PENDING_ON, STATE_PENDING_OFF],
    )
    async def test_state_machine_valid_transitions(
        self, setup_sensor_with_hass, target
    ):
        """Valid transitions from IDLE are accepted by the state machine."""
        sensor, hass = setup_sensor_with_hass

        await sensor.async_added_to_hass()

        # From IDLE, HEATING, PENDING_ON and PENDING_OFF are valid.
        assert sensor._state_machine.is_valid_transition(target)

    @pytest.mark.asyncio
    async def test_state_machine_callback_on_state_change(
        self, setup_sensor_with_hass
    ):
        """Test that state machine callbacks update sensor attributes."""
        sensor, hass = setup_sensor_with_hass

        await sensor.async_added_to_hass()

        # Manually trigger a state change through the state machine
        await sensor._state_machine.transition_to(STATE_HEATING)

        # Sensor attributes should be in sync
        assert sensor.native_value == STATE_HEATING
        assert sensor._state_machine.state == STATE_HEATING
