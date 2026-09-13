"""Tests for _evaluate_state when the relay entity is not yet available.

On startup (or whenever the relay entity has no state yet) ``_evaluate_state``
cannot know whether the relay is on or off.  Calling ``_apply_demand_logic``
in that situation would be unsafe because ``_is_on`` resolves a missing
entity to ``False``, so with demand ON the controller would try to turn the
relay on against an entity that does not exist (a futile/failing service
call during startup).

The correct behaviour is therefore: when the relay entity is unavailable,
set a safe placeholder state (IDLE), log a warning, and do NOT invoke
``_apply_demand_logic``.  The state machine self-heals once the relay becomes
available and a real demand/relay event arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from stove_controller.const import STATE_HEATING, STATE_IDLE


def _now() -> datetime:
    """Fixed timestamp for consistent testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


class TestEvaluateStateRelayUnavailable:
    """_evaluate_state with a missing relay entity must idle safely."""

    @pytest.mark.asyncio
    async def test_unavailable_relay_sets_idle(self, setup_sensor_hass):
        """Relay entity missing: state becomes IDLE."""
        sensor, hass = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True
        hass.states.get.return_value = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_unavailable_relay_does_not_apply_demand_logic(
        self, setup_sensor_hass
    ):
        """Relay entity missing: _apply_demand_logic must not be called."""
        sensor, hass = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True
        hass.states.get.return_value = None

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            await sensor._evaluate_state()

        mock_logic.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_relay_idles_even_with_demand_on(
        self, setup_sensor_hass
    ):
        """Demand ON but relay unavailable: conservative IDLE, no service call."""
        sensor, hass = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True
        hass.states.get.return_value = None
        hass.services.async_call = AsyncMock()

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        assert sensor._state == STATE_IDLE
        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_relay_does_not_raise(self, setup_sensor_hass):
        """Relay entity missing: _evaluate_state completes without exception."""
        sensor, hass = setup_sensor_hass()
        hass.states.get.return_value = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()  # should not raise

        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_available_relay_still_applies_demand_logic(self, setup_sensor_hass):
        """Positive control: relay available -> _apply_demand_logic is called."""
        sensor, hass = setup_sensor_hass()
        relay_state = MagicMock()
        relay_state.state = "off"
        hass.states.get.return_value = relay_state
        hass.states.is_state.return_value = False

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            await sensor._evaluate_state()

        mock_logic.assert_awaited_once()
