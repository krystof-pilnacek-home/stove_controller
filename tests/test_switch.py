"""Test switch platform for Stove Controller integration."""

from unittest.mock import MagicMock, AsyncMock, patch
import pytest
from homeassistant.const import STATE_ON, STATE_OFF

from stove_controller.switch import StoveDemandSwitch, async_setup_entry
from stove_controller.const import DOMAIN


class TestStoveDemandSwitch:
    """Test the Stove Demand Switch entity."""

    @pytest.fixture
    def switch(self):
        """Create a demand switch instance."""
        return StoveDemandSwitch("test_entry_id")

    def test_init(self, switch):
        """Test switch initialization."""
        assert switch._entry_id == "test_entry_id"
        assert switch._attr_name == "Stove Demand"
        assert switch._attr_unique_id == "test_entry_id_stove_demand"
        assert switch._attr_icon == "mdi:toggle-switch"
        assert switch._attr_should_poll is False
        assert switch._is_on is False

    def test_device_info(self, switch):
        """Test device info."""
        assert switch._attr_device_info["identifiers"] == {(DOMAIN, "test_entry_id")}
        assert switch._attr_device_info["name"] == "Stove Controller"
        assert switch._attr_device_info["manufacturer"] == "Custom"
        assert switch._attr_device_info["model"] == "A251 Controller"

    def test_is_on_property(self, switch):
        """Test is_on property."""
        assert switch.is_on is False
        switch._is_on = True
        assert switch.is_on is True

    @pytest.mark.asyncio
    async def test_async_turn_on(self, switch):
        """Test turning on the switch."""
        switch.hass = MagicMock()
        switch.hass.data = {DOMAIN: {"test_entry_id": {"sensor": MagicMock()}}}
        switch.async_write_ha_state = MagicMock()
        switch._notify_controller = AsyncMock()

        await switch.async_turn_on()

        assert switch._is_on is True
        switch.async_write_ha_state.assert_called_once()
        switch._notify_controller.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_turn_off(self, switch):
        """Test turning off the switch."""
        switch.hass = MagicMock()
        switch.hass.data = {DOMAIN: {"test_entry_id": {"sensor": MagicMock()}}}
        switch.async_write_ha_state = MagicMock()
        switch._notify_controller = AsyncMock()

        await switch.async_turn_off()

        assert switch._is_on is False
        switch.async_write_ha_state.assert_called_once()
        switch._notify_controller.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_added_to_hass_restores_state(self, switch):
        """Test that switch restores state on startup."""
        switch.hass = MagicMock()
        mock_state = MagicMock()
        mock_state.state = STATE_ON
        switch.async_get_last_state = AsyncMock(return_value=mock_state)
        switch.async_write_ha_state = MagicMock()

        await switch.async_added_to_hass()

        assert switch._is_on is True

    @pytest.mark.asyncio
    async def test_async_added_to_hass_no_previous_state(self, switch):
        """Test switch with no previous state."""
        switch.hass = MagicMock()
        switch.async_get_last_state = AsyncMock(return_value=None)

        await switch.async_added_to_hass()

        assert switch._is_on is False

    @pytest.mark.asyncio
    async def test_notify_controller_with_sensor(self, switch):
        """Test notifying controller when sensor is available."""
        mock_sensor = MagicMock()
        mock_sensor.handle_demand_change = AsyncMock()
        switch.hass = MagicMock()
        switch.hass.data = {DOMAIN: {"test_entry_id": {"sensor": mock_sensor}}}
        switch._is_on = True

        await switch._notify_controller()

        assert mock_sensor.handle_demand_change.called
        call_args = mock_sensor.handle_demand_change.call_args
        assert call_args[0][0] is True

    @pytest.mark.asyncio
    async def test_notify_controller_without_sensor(self, switch):
        """Test notifying controller when sensor is not available."""
        switch.hass = MagicMock()
        switch.hass.data = {DOMAIN: {"test_entry_id": {}}}
        switch._is_on = True

        with patch("stove_controller.switch._LOGGER") as mock_logger:
            await switch._notify_controller()
            mock_logger.warning.assert_called_once()


class TestAsyncSetupEntry:
    """Test the async_setup_entry function."""

    @pytest.mark.asyncio
    async def test_setup_entry(self, mock_hass, mock_config_entry):
        """Test setting up the switch platform."""
        async_add_entities = MagicMock()
        mock_hass.data = {}

        await async_setup_entry(mock_hass, mock_config_entry, async_add_entities)

        assert DOMAIN in mock_hass.data
        assert mock_config_entry.entry_id in mock_hass.data[DOMAIN]
        assert "switch" in mock_hass.data[DOMAIN][mock_config_entry.entry_id]

        async_add_entities.assert_called_once()
        entities = async_add_entities.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], StoveDemandSwitch)
