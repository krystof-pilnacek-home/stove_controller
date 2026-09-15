"""Test __init__.py for Stove Controller integration."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from stove_controller import async_setup_entry, async_unload_entry
from stove_controller.const import DOMAIN


class TestAsyncSetupEntry:
    """Test the async_setup_entry function."""

    @pytest.mark.asyncio
    async def test_setup_entry_success(self, mock_hass, mock_config_entry):
        """Test successful setup of integration entry."""
        mock_hass.config_entries.async_forward_entry_setups = AsyncMock()

        result = await async_setup_entry(mock_hass, mock_config_entry)

        assert result
        assert DOMAIN in mock_hass.data
        assert mock_config_entry.entry_id in mock_hass.data[DOMAIN]
        mock_hass.config_entries.async_forward_entry_setups.assert_called_once()

    @pytest.mark.asyncio
    async def test_setup_entry_syncs_demand(self, mock_hass, mock_config_entry):
        """Test that setup syncs demand state between switch and sensor."""
        mock_switch = MagicMock()
        mock_switch.is_on = True
        mock_switch.entity_id = "switch.stove_demand"

        mock_sensor = MagicMock()
        mock_sensor.sync_demand = AsyncMock()

        mock_hass.data = {}

        async def setup_platforms(*args, **kwargs):
            mock_hass.data.setdefault(DOMAIN, {})
            mock_hass.data[DOMAIN][mock_config_entry.entry_id] = {
                "switch": mock_switch,
                "sensor": mock_sensor,
            }

        mock_hass.config_entries.async_forward_entry_setups = setup_platforms

        await async_setup_entry(mock_hass, mock_config_entry)

        assert mock_sensor.sync_demand.called
        call_args = mock_sensor.sync_demand.call_args
        assert call_args[0][0]
        assert call_args[0][1] == "switch.stove_demand"

    @pytest.mark.asyncio
    async def test_setup_entry_no_sync_without_switch_or_sensor(
        self, mock_hass, mock_config_entry
    ):
        """Test that setup doesn't fail when switch or sensor is not available."""
        mock_hass.data = {DOMAIN: {mock_config_entry.entry_id: {}}}
        mock_hass.config_entries.async_forward_entry_setups = AsyncMock()

        result = await async_setup_entry(mock_hass, mock_config_entry)

        assert result
class TestAsyncUnloadEntry:
    """Test the async_unload_entry function."""

    @pytest.mark.asyncio
    async def test_unload_entry_success(self, mock_hass, mock_config_entry):
        """Test successful unload of integration entry."""
        mock_hass.data = {DOMAIN: {mock_config_entry.entry_id: {}}}
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        result = await async_unload_entry(mock_hass, mock_config_entry)

        assert result
        mock_hass.config_entries.async_unload_platforms.assert_called_once()
        assert mock_config_entry.entry_id not in mock_hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_unload_entry_failure(self, mock_hass, mock_config_entry):
        """Test failed unload of integration entry."""
        mock_hass.data = {DOMAIN: {mock_config_entry.entry_id: {}}}
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

        result = await async_unload_entry(mock_hass, mock_config_entry)

        assert not result
        mock_hass.config_entries.async_unload_platforms.assert_called_once()
        assert mock_config_entry.entry_id in mock_hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_unload_entry_data_not_present(self, mock_hass, mock_config_entry):
        """Test unload when entry data is not present."""
        mock_hass.data = {DOMAIN: {}}
        mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        result = await async_unload_entry(mock_hass, mock_config_entry)

        assert result
