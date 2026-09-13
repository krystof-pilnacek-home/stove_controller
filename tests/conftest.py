"""Fixtures for Stove Controller tests."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from stove_controller.const import DOMAIN
from stove_controller.sensor import StoveControllerSensor


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


@pytest.fixture
def make_sensor():
    """Factory fixture to create a StoveControllerSensor with test config."""

    def _make(
        relay_entity: str = "switch.test_relay",
        min_on_min: int = 30,
        min_off_min: int = 25,
    ) -> StoveControllerSensor:
        return StoveControllerSensor(
            entry_id="test_entry_id",
            relay_entity=relay_entity,
            min_on_min=min_on_min,
            min_off_min=min_off_min,
        )

    return _make


@pytest.fixture
async def setup_sensor_hass(make_sensor, monkeypatch):
    """Factory fixture to set up a sensor with a mock hass.

    Returns a callable that accepts the same kwargs as ``make_sensor``
    and returns a ``(sensor, hass)`` tuple.  Uses ``monkeypatch.setattr``
    so that mocking base-class methods passes mypy.

    On teardown, cancels any pending wait task on every sensor created
    via the factory so the HACC ``verify_cleanup`` plugin does not flag
    lingering ``_wait_and_execute`` tasks.
    """
    created: list[StoveControllerSensor] = []

    def _setup(
        relay_entity: str = "switch.test_relay",
        min_on_min: int = 30,
        min_off_min: int = 25,
    ) -> tuple[StoveControllerSensor, MagicMock]:
        sensor = make_sensor(
            relay_entity=relay_entity,
            min_on_min=min_on_min,
            min_off_min=min_off_min,
        )

        hass = MagicMock()
        hass.states = MagicMock()
        hass.states.get = MagicMock()
        hass.states.is_state = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.data = {
            DOMAIN: {"test_entry_id": {"sensor": sensor, "switch": MagicMock()}}
        }

        sensor.hass = hass
        sensor._state_machine.hass = hass
        monkeypatch.setattr(sensor, "async_on_remove", MagicMock())
        monkeypatch.setattr(sensor, "async_write_ha_state", MagicMock())
        monkeypatch.setattr(
            sensor, "async_get_last_state", AsyncMock(return_value=None)
        )

        created.append(sensor)
        return sensor, hass

    yield _setup

    # Cancel and await any pending wait task so it does not linger after the
    # test.  The tasks must be awaited to completion (not just cancelled) so
    # the HACC verify_cleanup plugin does not flag them as lingering.
    wait_tasks = [
        sensor._wait_task
        for sensor in created
        if sensor._wait_task is not None and not sensor._wait_task.done()
    ]
    for sensor in created:
        # The wait timer lives on the state machine after the refactor; the
        # sensor no longer exposes _cancel_wait.  Stop the periodic countdown
        # tracker too so no time listener lingers.
        sensor._state_machine.cancel_wait()
        sensor._stop_periodic_update()
    for task in wait_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
