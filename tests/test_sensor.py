"""Test sensor platform for Stove Controller integration."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from stove_controller.const import (
    DOMAIN,
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)
from stove_controller.sensor import (
    StoveControllerSensor,
    StoveLastOffSensor,
    StoveLastOnSensor,
    StoveRemainingTimeSensor,
    async_setup_entry,
)


@pytest.fixture
def sensor():
    """Create a sensor instance with default values."""
    return StoveControllerSensor(
        "test_entry_id",
        "switch.test_relay",
        min_on_min=30,
        min_off_min=25,
    )


class TestStoveControllerSensor:
    """Test the Stove Controller Sensor entity."""

    def test_init(self, sensor):
        """Test sensor initialization."""
        assert sensor._entry_id == "test_entry_id"
        assert sensor._relay_entity == "switch.test_relay"
        assert sensor._min_on_duration == 30 * 60
        assert sensor._min_off_duration == 25 * 60
        assert sensor._attr_name == "Stove Controller"
        assert sensor._attr_unique_id == "test_entry_id_stove_controller"
        assert sensor._attr_icon == "mdi:fire"
        assert not sensor._attr_should_poll
        assert sensor.native_value == STATE_IDLE
        assert not sensor._state_machine.demand_on
    def test_device_info(self, sensor):
        """Test device info."""
        assert sensor._attr_device_info["identifiers"] == {(DOMAIN, "test_entry_id")}
        assert sensor._attr_device_info["name"] == "Stove Controller"
        assert sensor._attr_device_info["manufacturer"] == "Custom"
        assert sensor._attr_device_info["model"] == "A251 Controller"

    def test_native_value(self, sensor):
        """Test native_value property."""
        assert sensor.native_value == STATE_IDLE
        sensor._state_machine._state = STATE_HEATING
        assert sensor.native_value == STATE_HEATING

    def test_extra_state_attributes_basic(self, sensor):
        """Test basic extra state attributes."""
        attrs = sensor.extra_state_attributes
        assert attrs["relay_entity"] == "switch.test_relay"
        assert not attrs["demand_on"]
        assert attrs["min_on_duration_min"] == 30
        assert attrs["min_off_duration_min"] == 25
        assert not attrs["in_grace_period"]
        assert attrs["time_remaining_sec"] == 0

    def test_extra_state_attributes_with_demand_entity(self, sensor):
        """Test extra state attributes with demand entity ID."""
        sensor._demand_entity_id = "switch.stove_demand"
        attrs = sensor.extra_state_attributes
        assert attrs["demand_entity"] == "switch.stove_demand"

    def test_extra_state_attributes_with_timestamps(self, sensor):
        """Test extra state attributes with timestamps."""
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt_util.UTC)
        sensor._state_machine._last_on = now
        sensor._state_machine._last_off = now

        attrs = sensor.extra_state_attributes
        assert attrs["last_on"] == now.isoformat()
        assert attrs["last_off"] == now.isoformat()

    def test_extra_state_attributes_with_wait_time(self, sensor):
        """Test extra state attributes with wait time remaining."""
        sensor._state_machine._wait_until = dt_util.now() + timedelta(seconds=100)
        attrs = sensor.extra_state_attributes
        assert attrs["time_remaining_sec"] > 0

    def test_in_grace_period_true(self, sensor):
        """Test in_grace_period is True during pending states."""
        sensor._state_machine._state = STATE_PENDING_ON
        attrs = sensor.extra_state_attributes
        assert attrs["in_grace_period"]
        sensor._state_machine._state = STATE_PENDING_OFF
        attrs = sensor.extra_state_attributes
        assert attrs["in_grace_period"]
    def test_in_grace_period_false(self, sensor):
        """Test in_grace_period is False during non-pending states."""
        sensor._state_machine._state = STATE_IDLE
        attrs = sensor.extra_state_attributes
        assert not attrs["in_grace_period"]
        sensor._state_machine._state = STATE_HEATING
        attrs = sensor.extra_state_attributes
        assert not attrs["in_grace_period"]
class TestSensorLifecycle:
    """Test sensor lifecycle methods."""

    @pytest.mark.asyncio
    async def test_async_added_to_hass_restores_state(self, setup_sensor_hass):
        """Test that sensor restores state from previous state."""
        sensor, hass = setup_sensor_hass()
        hass.states.is_state.return_value = True  # relay ON during HEATING

        mock_state = MagicMock()
        mock_state.state = STATE_HEATING
        mock_state.attributes = {
            "demand_on": True,
            "last_on": "2024-01-15T10:00:00+00:00",
            "last_off": "2024-01-15T09:00:00+00:00",
        }
        sensor.async_get_last_state = AsyncMock(return_value=mock_state)

        with patch(
            "homeassistant.util.dt.parse_datetime",
            side_effect=dt_util.parse_datetime,
        ):
            await sensor.async_added_to_hass()

        assert sensor.native_value == STATE_HEATING
        assert sensor._state_machine.demand_on
        assert sensor._state_machine.last_on is not None
        assert sensor._state_machine.last_off is not None

    @pytest.mark.asyncio
    async def test_async_added_to_hass_no_previous_state(self, setup_sensor_hass):
        """Test sensor with no previous state."""
        sensor, hass = setup_sensor_hass()
        hass.states.is_state.return_value = False  # relay OFF

        await sensor.async_added_to_hass()

        assert sensor.native_value == STATE_IDLE

    @pytest.mark.asyncio
    async def test_async_will_remove_from_hass(self, sensor):
        """Test cleanup on removal."""
        sensor._state_machine._wait_task = MagicMock()
        sensor._state_machine._wait_task.done.return_value = False
        sensor._update_unsub = MagicMock()

        await sensor.async_will_remove_from_hass()

        assert sensor._state_machine._wait_task is None
        assert sensor._state_machine._wait_until is None
        assert sensor._update_unsub is None


class TestComputeRemaining:
    """Test _compute_remaining method."""

    @pytest.fixture
    def sensor(self):
        """Create sensor instance."""
        return StoveControllerSensor("test_entry_id", "switch.test_relay", 30, 25)

    def test_none_last_time_returns_full_duration(self, sensor):
        """Test that None last_time returns full duration."""
        result = sensor._state_machine.compute_remaining(None, 100)
        assert result == 100

    def test_elapsed_less_than_duration(self, sensor):
        """Test remaining time when elapsed < duration."""
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt_util.UTC)
        last_time = now - timedelta(seconds=50)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sensor._state_machine.compute_remaining(last_time, 100)

        assert result == 50

    def test_elapsed_more_than_duration(self, sensor):
        """Test remaining time when elapsed > duration."""
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=dt_util.UTC)
        last_time = now - timedelta(seconds=150)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sensor._state_machine.compute_remaining(last_time, 100)

        assert result == 0


class TestDeviceInfo:
    """Test device info."""

    def test_device_info(self):
        """Test device info is correct."""
        sensor = StoveControllerSensor("test_entry", "switch.test", 30, 25)
        info = sensor._attr_device_info
        assert info["identifiers"] == {(DOMAIN, "test_entry")}
        assert info["name"] == "Stove Controller"
        assert info["manufacturer"] == "Custom"
        assert info["model"] == "A251 Controller"


class TestAsyncSetupEntry:
    """Test the async_setup_entry function."""

    @pytest.mark.asyncio
    async def test_setup_entry(self, mock_hass, mock_config_entry):
        """Test setting up the sensor platform."""
        async_add_entities = MagicMock()
        mock_hass.data = {}

        await async_setup_entry(mock_hass, mock_config_entry, async_add_entities)

        assert DOMAIN in mock_hass.data
        assert mock_config_entry.entry_id in mock_hass.data[DOMAIN]
        assert "sensor" in mock_hass.data[DOMAIN][mock_config_entry.entry_id]

        async_add_entities.assert_called_once()
        entities = async_add_entities.call_args[0][0]
        assert len(entities) == 4
        assert isinstance(entities[0], StoveControllerSensor)
        assert isinstance(entities[1], StoveRemainingTimeSensor)
        assert isinstance(entities[2], StoveLastOnSensor)
        assert isinstance(entities[3], StoveLastOffSensor)

    @pytest.mark.asyncio
    async def test_setup_entry_with_options(
        self, mock_hass, mock_config_entry_with_options
    ):
        """Test setting up sensor with options overriding defaults."""
        async_add_entities = MagicMock()
        mock_hass.data = {}

        await async_setup_entry(
            mock_hass, mock_config_entry_with_options, async_add_entities
        )

        entities = async_add_entities.call_args[0][0]
        sensor = entities[0]
        assert sensor._min_on_duration == 45 * 60
        assert sensor._min_off_duration == 30 * 60
