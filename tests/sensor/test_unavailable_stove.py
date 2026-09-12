"""Tests asserting correct behaviour on persistent service-call failure.

These tests will FAIL on the current (buggy) code because _do_turn_on /
_do_turn_off / _wait_and_execute enter unbounded recursion when the relay
service is unavailable.  Once the recursion is fixed they will pass without
modification.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)

_VALID_STATES = {STATE_IDLE, STATE_HEATING, STATE_PENDING_ON, STATE_PENDING_OFF}


def _now() -> datetime:
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


def _failing_async_call(error_msg: str = "Service not available") -> AsyncMock:
    return AsyncMock(side_effect=RuntimeError(error_msg))


class TestServiceFailureRecursion:
    """Assert correct behaviour when the relay service is persistently unavailable."""

    @pytest.mark.asyncio
    async def test_do_turn_on_persistent_failure(self, setup_sensor_hass):
        """_do_turn_on completes normally, logs the error, and ends in a valid state.

        On the buggy code this raises RecursionError.
        """
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = True
        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=30)
        hass.services.async_call = _failing_async_call("switch.turn_on not available")

        with patch("homeassistant.util.dt.now", return_value=_now()):
            with patch("stove_controller.sensor._LOGGER") as mock_logger:
                await sensor._do_turn_on()

        mock_logger.error.assert_called()
        assert sensor._state in _VALID_STATES
        assert sensor._state != STATE_HEATING

    @pytest.mark.asyncio
    async def test_do_turn_off_persistent_failure(self, setup_sensor_hass):
        """_do_turn_off completes normally, logs the error, and ends in a valid state.

        On the buggy code this raises RecursionError.
        """
        sensor, hass = setup_sensor_hass(min_on_min=30)
        sensor._demand_on = False
        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=35)
        hass.services.async_call = _failing_async_call("switch.turn_off not available")

        with patch("homeassistant.util.dt.now", return_value=_now()):
            with patch("stove_controller.sensor._LOGGER") as mock_logger:
                await sensor._do_turn_off()

        mock_logger.error.assert_called()
        assert sensor._state in _VALID_STATES
        assert sensor._state != STATE_IDLE

    @pytest.mark.asyncio
    async def test_handle_demand_change_triggers_turn_on_failure(
        self, setup_sensor_hass
    ):
        """handle_demand_change completes normally when the relay service is down.

        On the buggy code this raises RecursionError.
        """
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = False
        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=30)
        hass.services.async_call = _failing_async_call("switch.turn_on not available")

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state in _VALID_STATES
        assert sensor._state != STATE_HEATING

    @pytest.mark.asyncio
    async def test_wait_and_execute_propagates_failure(self, setup_sensor_hass):
        """_wait_and_execute completes normally when its callback always fails.

        On the buggy code this raises RecursionError.
        """
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = True
        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=30)
        hass.services.async_call = _failing_async_call("switch.turn_on not available")

        with patch("homeassistant.util.dt.now", return_value=_now()):
            with patch("stove_controller.sensor._LOGGER") as mock_logger:
                await sensor._wait_and_execute(0, sensor._do_turn_on)

        # _do_turn_on catches its own RuntimeError and logs via _LOGGER.error;
        # the exception never reaches _wait_and_execute's except block.
        mock_logger.error.assert_called()
        assert sensor._wait_task is None
        assert sensor._wait_until is None
        assert sensor._state in _VALID_STATES
