"""Tests for the CORRECT behaviour when the relay is unavailable at evaluate time.

Two distinct requirements:

1. Safe placeholder (already correct, locked in here): when the relay entity
   has no state at evaluate time, ``_evaluate_state`` must set a safe IDLE
   placeholder, make no service call, and NOT invoke ``_apply_demand_logic``
   (which would treat a missing relay as OFF and, with demand ON, attempt a
   futile turn-on during startup).

2. Demand recovery (NOT yet implemented, these tests FAIL): on HA restart the
   relay entity reports its first state via a state-change event with
   ``old_state=None``.  The controller must then re-evaluate demand so that a
   previously-idled controller resumes heating / pending.  The current code
   skips ALL ``old_state=None`` events (the branch's Bug A fix), which
   preserves timestamps but permanently drops demand when the relay was
   unavailable at setup.  Correct behaviour: on ``old_state=None``, preserve
   timestamps but STILL re-evaluate demand logic.

The recovery tests intentionally contradict the branch's
``test_old_state_none_no_state_re_evaluation`` (which over-asserts that
``_apply_demand_logic`` is never called for ``old_state=None``).  That test
encodes an over-correction; the correct fix must both preserve timestamps
and recover demand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_ON,
)


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


class TestEvaluateStateRelayUnavailableSafePlaceholder:
    """The IDLE placeholder for an unavailable relay is correct; lock it in.

    These PASS against the current code and must keep passing after any fix.
    """

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
    async def test_unavailable_relay_makes_no_service_call(self, setup_sensor_hass):
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


class TestRelayUnavailableDemandRecovery:
    """Demand must be recovered when the relay appears after being unavailable.

    These assert the CORRECT behaviour and FAIL against the current code,
    which skips all ``old_state=None`` relay events and thus leaves a
    demand-ON controller stuck in IDLE after the relay comes back online.
    """

    @pytest.mark.asyncio
    async def test_relay_appearing_off_old_state_none_reapplies_demand(
        self, setup_sensor_hass
    ):
        """Relay appears (OFF, old_state=None) with demand ON -> PENDING_ON.

        Setup mirrors HA restart: relay unavailable at evaluate time (IDLE),
        then the relay entity reports its initial OFF state with old_state=None.
        Correct behaviour: demand is re-evaluated -> PENDING_ON (full min_off
        wait because _last_off is None).  Current code skips the event and
        stays IDLE.
        """
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = True
        sensor._last_off = None
        # Relay unavailable at evaluate time -> safe IDLE placeholder.
        hass.states.get.return_value = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()
        assert sensor._state == STATE_IDLE  # placeholder is correct

        # Relay now appears and reports OFF with old_state=None (HA restart).
        relay_state = MagicMock()
        relay_state.state = STATE_OFF
        hass.states.get.return_value = relay_state
        hass.states.is_state.return_value = False
        event = _make_relay_event(new_state_value=STATE_OFF, old_state_value=None)

        try:
            with patch("homeassistant.util.dt.now", return_value=_now()):
                await sensor._on_relay_change(event)
        finally:
            sensor._cancel_wait()

        # Demand ON + relay OFF + no prior _last_off -> must wait min_off.
        assert sensor._state == STATE_PENDING_ON

    @pytest.mark.asyncio
    async def test_relay_appearing_on_old_state_none_resumes_heating(
        self, setup_sensor_hass
    ):
        """Relay appears (ON, old_state=None) with demand ON -> HEATING.

        The relay was on before restart and reports ON with old_state=None.
        Correct behaviour: re-evaluate -> demand ON + relay ON -> HEATING.
        Current code skips the event and stays IDLE.
        """
        sensor, hass = setup_sensor_hass()
        sensor._demand_on = True
        # Relay unavailable at evaluate time -> safe IDLE placeholder.
        hass.states.get.return_value = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()
        assert sensor._state == STATE_IDLE  # placeholder is correct

        # Relay now appears and reports ON with old_state=None (HA restart).
        relay_state = MagicMock()
        relay_state.state = STATE_ON
        hass.states.get.return_value = relay_state
        hass.states.is_state.return_value = True
        event = _make_relay_event(new_state_value=STATE_ON, old_state_value=None)

        try:
            with patch("homeassistant.util.dt.now", return_value=_now()):
                await sensor._on_relay_change(event)
        finally:
            sensor._cancel_wait()

        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_relay_appearing_old_state_none_re_evaluates_demand(
        self, setup_sensor_hass
    ):
        """Relay appears with old_state=None: _apply_demand_logic must be called.

        This is the direct contradiction of the branch's
        test_old_state_none_no_state_re_evaluation: that test over-asserts no
        re-evaluation.  Correct behaviour re-evaluates demand (without updating
        timestamps) so demand is not lost on restart.
        """
        sensor, hass = setup_sensor_hass()
        sensor._demand_on = True
        sensor._last_off = None
        hass.states.get.return_value = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        relay_state = MagicMock()
        relay_state.state = STATE_OFF
        hass.states.get.return_value = relay_state
        hass.states.is_state.return_value = False
        event = _make_relay_event(new_state_value=STATE_OFF, old_state_value=None)

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            try:
                await sensor._on_relay_change(event)
            finally:
                sensor._cancel_wait()

        mock_logic.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_relay_appearing_old_state_none_preserves_timestamps(
        self, setup_sensor_hass
    ):
        """Recovery must NOT overwrite timestamps (Bug A stays fixed).

        Correct behaviour combines both: re-evaluate demand AND preserve
        _last_on / _last_off on old_state=None.  The current code preserves
        timestamps but skips re-evaluation; the fix must keep timestamps while
        re-evaluating.
        """
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = True
        original_last_off = _now() - timedelta(minutes=30)
        sensor._last_off = original_last_off
        sensor._last_on = None
        hass.states.get.return_value = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        # Relay appears OFF with old_state=None; last_off 30 min ago means the
        # min_off wait has elapsed -> HEATING (no timestamp update needed).
        relay_state = MagicMock()
        relay_state.state = STATE_OFF
        hass.states.get.return_value = relay_state
        hass.states.is_state.return_value = False
        event = _make_relay_event(new_state_value=STATE_OFF, old_state_value=None)

        try:
            with patch("homeassistant.util.dt.now", return_value=_now()):
                await sensor._on_relay_change(event)
        finally:
            sensor._cancel_wait()

        # Demand recovered ...
        assert sensor._state == STATE_HEATING
        # ... without corrupting timestamps.
        assert sensor._last_off == original_last_off
        assert sensor._last_on is None


class TestEvaluateStatePositiveControl:
    """Positive control: when the relay IS available, demand logic is applied."""

    @pytest.mark.asyncio
    async def test_available_relay_applies_demand_logic(self, setup_sensor_hass):
        """Relay available + demand ON + relay OFF + no _last_off -> PENDING_ON."""
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = True
        sensor._last_off = None
        relay_state = MagicMock()
        relay_state.state = STATE_OFF
        hass.states.get.return_value = relay_state
        hass.states.is_state.return_value = False

        with patch("homeassistant.util.dt.now", return_value=_now()):
            try:
                await sensor._evaluate_state()
            finally:
                sensor._cancel_wait()

        assert sensor._state == STATE_PENDING_ON
