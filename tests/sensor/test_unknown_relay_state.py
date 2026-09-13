"""Tests for relay state-change events with non on/off (unknown/unavailable) states.

When the relay entity transitions to a state other than STATE_ON / STATE_OFF
(e.g. ``unavailable`` or ``unknown`` because the relay lost communication),
``_on_relay_change`` must:

* NOT overwrite ``_last_on`` / ``_last_off`` with the current time, and
* NOT re-enter ``_apply_demand_logic``.

Re-evaluating the state machine against a non-on/off relay would be unsafe:
``_is_on`` (``hass.states.is_state``) returns ``False`` for an unavailable
entity, so ``_apply_demand_logic`` would treat the relay as OFF and, with
demand ON, could spuriously attempt to turn the relay on again (short-cycle
risk).  The correct behaviour is therefore to log a warning and leave the
controller state and timestamps untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from stove_controller.const import STATE_HEATING


def _now() -> datetime:
    """Fixed timestamp for consistent testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


def _make_relay_event(
    new_state_value: str | None,
    old_state_value: str | None = None,
) -> MagicMock:
    """Build a mock state-change event for _on_relay_change."""
    event = MagicMock()
    data: dict = {}
    if new_state_value is not None:
        mock_new = MagicMock()
        mock_new.state = new_state_value
        data["new_state"] = mock_new
    else:
        data["new_state"] = None
    if old_state_value is not None:
        mock_old = MagicMock()
        mock_old.state = old_state_value
        data["old_state"] = mock_old
    else:
        data["old_state"] = None
    event.data = data
    return event


class TestUnknownRelayState:
    """Relay transitions to unavailable/unknown must not corrupt state or re-evaluate."""

    @pytest.mark.asyncio
    async def test_unavailable_preserves_timestamps(self, setup_sensor_hass):
        """Relay ON -> unavailable: _last_on is not overwritten, _last_off untouched."""
        sensor, _ = setup_sensor_hass()
        original_last_on = _now() - timedelta(minutes=5)
        original_last_off = _now() - timedelta(minutes=20)
        sensor._last_on = original_last_on
        sensor._last_off = original_last_off
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_ON
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._last_on == original_last_on
        assert sensor._last_off == original_last_off
        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_unknown_preserves_timestamps(self, setup_sensor_hass):
        """Relay ON -> unknown: timestamps and state unchanged."""
        sensor, _ = setup_sensor_hass()
        original_last_on = _now() - timedelta(minutes=3)
        sensor._last_on = original_last_on
        sensor._last_off = None
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNKNOWN, old_state_value=STATE_ON
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._last_on == original_last_on
        assert sensor._last_off is None
        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_unavailable_does_not_re_evaluate(self, setup_sensor_hass):
        """Relay ON -> unavailable: _apply_demand_logic must not be called."""
        sensor, _ = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_ON
        )

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            await sensor._on_relay_change(event)

        mock_logic.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_does_not_trigger_service_call(self, setup_sensor_hass):
        """Relay ON -> unavailable: no relay service call is made."""
        sensor, hass = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True
        hass.services.async_call = AsyncMock()

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_ON
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_from_off_preserves_last_off(self, setup_sensor_hass):
        """Relay OFF -> unavailable: _last_off is not overwritten."""
        sensor, _ = setup_sensor_hass()
        original_last_off = _now() - timedelta(minutes=10)
        sensor._last_off = original_last_off
        sensor._last_on = None

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_OFF
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._last_off == original_last_off
        assert sensor._last_on is None

    @pytest.mark.asyncio
    async def test_real_on_transition_still_updates_timestamps(self, setup_sensor_hass):
        """Positive control: OFF -> ON still updates _last_on and re-evaluates."""
        sensor, hass = setup_sensor_hass()
        sensor._last_on = None
        sensor._last_off = _now() - timedelta(minutes=5)
        hass.states.is_state.return_value = False

        event = _make_relay_event(new_state_value=STATE_ON, old_state_value=STATE_OFF)

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            with patch("homeassistant.util.dt.now", return_value=_now()):
                await sensor._on_relay_change(event)

        assert sensor._last_on == _now()
        mock_logic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_real_off_transition_still_updates_timestamps(
        self, setup_sensor_hass
    ):
        """Positive control: ON -> OFF still updates _last_off and re-evaluates."""
        sensor, hass = setup_sensor_hass()
        sensor._last_on = _now() - timedelta(minutes=5)
        sensor._last_off = None
        hass.states.is_state.return_value = True

        event = _make_relay_event(new_state_value=STATE_OFF, old_state_value=STATE_ON)

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            with patch("homeassistant.util.dt.now", return_value=_now()):
                await sensor._on_relay_change(event)

        assert sensor._last_off == _now()
        mock_logic.assert_awaited_once()
