"""Fixtures for Stove Controller tests."""

from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


@pytest.fixture
def mock_hass():
    """Create a mock Home Assistant instance."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {}
    hass.states = MagicMock()
    hass.services = MagicMock()
    hass.config_entries = MagicMock()
    return hass


@pytest.fixture
def mock_config_entry():
    """Create a mock config entry."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.data = {
        "relay_entity": "switch.test_relay",
        "min_on_duration": 30,
        "min_off_duration": 25,
    }
    entry.options = {}
    return entry


@pytest.fixture
def mock_config_entry_with_options():
    """Create a mock config entry with options."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.data = {
        "relay_entity": "switch.test_relay",
    }
    entry.options = {
        "min_on_duration": 45,
        "min_off_duration": 30,
    }
    return entry
