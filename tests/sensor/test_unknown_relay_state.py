"""Tests for the CORRECT behaviour when the relay reports a non on/off state.

When the relay entity transitions to a state other than STATE_ON / STATE_OFF
(e.g. ``unavailable`` or ``unknown`` because the relay lost communication),
``_on_relay_change`` must NOT leave the controller reporting HEATING.  The
stove is no longer confirmed to be heating, so the controller must transition
to a safe placeholder state (IDLE), consistent with ``_evaluate_state``'s
handling of an unavailable relay.

Correct behaviour for a non-on/off relay event:
* transition out of HEATING / PENDING_* to IDLE,
* preserve ``_last_on`` / ``_last_off`` (do not corrupt anti-short-cycle timers),
* make NO relay service call (re-evaluating against an unavailable relay,
  which ``_is_on`` resolves to False, would spuriously try to turn it on),
* write the HA state and push to sub-sensors so the UI reflects reality.

These tests assert that correct behaviour.  They FAIL against the current
(unfixed) code, which only logs a warning and returns, leaving the controller
in HEATING with a dead relay.
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
from stove_controller.const import STATE_HEATING, STATE_IDLE


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
    """A relay going unavailable/unknown must move the controller to IDLE.

    All tests in this class assert the CORRECT behaviour and FAIL against the
    current code (which leaves the controller in HEATING).
    """

    @pytest.mark.asyncio
    async def test_unavailable_transitions_to_idle(self, setup_sensor_hass):
        """Relay ON -> unavailable while heating: controller must become IDLE."""
        sensor, _ = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_ON
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_unknown_transitions_to_idle(self, setup_sensor_hass):
        """Relay ON -> unknown while heating: controller must become IDLE."""
        sensor, _ = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNKNOWN, old_state_value=STATE_ON
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_unavailable_preserves_timestamps_and_idles(
        self, setup_sensor_hass
    ):
        """Relay ON -> unavailable: timestamps preserved AND state becomes IDLE."""
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
        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_unavailable_makes_no_service_call_and_idles(
        self, setup_sensor_hass
    ):
        """Relay ON -> unavailable: no service call AND state becomes IDLE."""
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
        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_unavailable_writes_state_and_idles(self, setup_sensor_hass):
        """Relay ON -> unavailable: HA state is written AND state becomes IDLE."""
        sensor, _ = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_ON
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        # The UI must be refreshed to reflect the dead relay.
        assert sensor.async_write_ha_state.called
        assert sensor._state == STATE_IDLE

    @pytest.mark.asyncio
    async def test_unavailable_from_pending_on_idles(self, setup_sensor_hass):
        """Relay -> unavailable while PENDING_ON: controller must become IDLE."""
        sensor, _ = setup_sensor_hass()
        sensor._state = STATE_HEATING  # any non-IDLE active state
        sensor._demand_on = True

        event = _make_relay_event(
            new_state_value=STATE_UNAVAILABLE, old_state_value=STATE_OFF
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._state == STATE_IDLE


class TestUnknownRelayStatePositiveControls:
    """Positive controls proving the test harness handles normal transitions.

    These PASS against the current code and exist only to show that the
    failing tests above fail because of the bug, not because of a broken
    harness.  The existing repo tests already cover normal transitions.
    """

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
