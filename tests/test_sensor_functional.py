"""Functional tests for StoveControllerSensor.

Covers scenarios where wait is triggered on ON/OFF transitions,
and simple cases where no wait is necessary.

Scenarios:
- S1: Simple ON/OFF with no delays
- S2: ON with min_off delay
- S3: OFF with min_on delay
- S4: Demand reversal during wait
- S5: Boundary conditions (exactly at min_duration)
- S6: State restoration with timers
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON

from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)

# Helpers


def _now() -> datetime:
    """Fixed timestamp for consistent testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


# =============================================================================
# Scenario 1: Simple Cases - No Wait Needed
# =============================================================================


class TestScenario1SimpleCases:
    """Simple cases where no wait is necessary."""

    @pytest.mark.asyncio
    async def test_turn_on_no_delay_relay_off(self, setup_sensor_hass):
        """Demand ON, relay OFF, min_off elapsed -> immediate HEATING."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        # Relay is OFF
        hass.states.is_state.return_value = False
        # Last off was 30 minutes ago (> 25 min)
        sensor._last_off = _now() - timedelta(minutes=30)
        sensor._last_on = None

        hass.services.async_call.reset_mock()
        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_HEATING
        assert sensor._demand_on is True
        # Should have called turn_on service
        hass.services.async_call.assert_awaited_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "switch"
        assert call_args[0][1] == "turn_on"

    @pytest.mark.asyncio
    async def test_turn_off_no_delay_relay_on(self, setup_sensor_hass):
        """Demand OFF, relay ON, min_on elapsed -> immediate IDLE."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        # Relay is ON
        hass.states.is_state.return_value = True
        # Last on was 35 minutes ago (> 30 min)
        sensor._last_on = _now() - timedelta(minutes=35)
        sensor._last_off = None
        # Initial demand is ON
        sensor._demand_on = True

        hass.services.async_call.reset_mock()
        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_IDLE
        assert sensor._demand_on is False
        # Should have called turn_off service
        hass.services.async_call.assert_awaited_once()
        call_args = hass.services.async_call.call_args
        assert call_args[0][0] == "switch"
        assert call_args[0][1] == "turn_off"

    @pytest.mark.asyncio
    async def test_demand_on_relay_already_on(self, setup_sensor_hass):
        """Demand ON, relay already ON -> just set HEATING."""
        sensor, hass = setup_sensor_hass()

        hass.states.is_state.return_value = True  # relay ON
        sensor._demand_on = False

        await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_HEATING
        assert sensor._demand_on is True
        # No service call needed
        hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_demand_off_relay_already_off(self, setup_sensor_hass):
        """Demand OFF, relay already OFF -> just set IDLE."""
        sensor, hass = setup_sensor_hass()

        hass.states.is_state.return_value = False  # relay OFF
        sensor._demand_on = True

        await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_IDLE
        assert sensor._demand_on is False
        # No service call needed
        hass.services.async_call.assert_not_awaited()


# =============================================================================
# Scenario 2: Wait Triggered Cases
# =============================================================================


class TestScenario2WaitTriggered:
    """Cases where wait timers are triggered."""

    @pytest.mark.asyncio
    async def test_turn_on_with_min_off_delay(self, setup_sensor_hass):
        """Demand ON, relay OFF, min_off NOT elapsed -> PENDING_ON with wait."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        # Relay is OFF
        hass.states.is_state.return_value = False
        # Last off was only 10 minutes ago (< 25 min)
        sensor._last_off = _now() - timedelta(minutes=10)
        sensor._last_on = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_PENDING_ON
        assert sensor._demand_on is True
        assert sensor._wait_task is not None
        assert sensor._wait_until is not None
        # Should NOT have called turn_on service yet
        hass.services.async_call.assert_not_awaited()

        # Wait should complete after 15 minutes (25 - 10)
        expected_wait = timedelta(minutes=15)
        assert sensor._wait_until == _now() + expected_wait

    @pytest.mark.asyncio
    async def test_turn_off_with_min_on_delay(self, setup_sensor_hass):
        """Demand OFF, relay ON, min_on NOT elapsed -> PENDING_OFF with wait."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        # Relay is ON
        hass.states.is_state.return_value = True
        # Last on was only 15 minutes ago (< 30 min)
        sensor._last_on = _now() - timedelta(minutes=15)
        sensor._last_off = None
        # Initial demand is ON
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_PENDING_OFF
        assert sensor._demand_on is False
        assert sensor._wait_task is not None
        assert sensor._wait_until is not None
        # Should NOT have called turn_off service yet
        hass.services.async_call.assert_not_awaited()

        # Wait should complete after 15 minutes (30 - 15)
        expected_wait = timedelta(minutes=15)
        assert sensor._wait_until == _now() + expected_wait

    @pytest.mark.asyncio
    async def test_wait_completion_turns_on_relay(self, setup_sensor_hass):
        """After PENDING_ON wait completes, relay turns ON."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=10)
        sensor._demand_on = False

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_PENDING_ON
        assert sensor._wait_task is not None

        # Simulate wait completion: cancel the real wait task (instead of
        # orphaning it by nilling the reference) so no _wait_and_execute
        # task lingers after the test.
        sensor._state_machine._cancel_wait()
        assert sensor._wait_task is None

        hass.services.async_call.reset_mock()
        with patch(
            "homeassistant.util.dt.now", return_value=_now() + timedelta(minutes=15)
        ):
            # Call the completion callback directly
            await sensor._state_machine._complete_turn_on()

        assert sensor._state == STATE_HEATING
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wait_completion_turns_off_relay(self, setup_sensor_hass):
        """After PENDING_OFF wait completes, relay turns OFF."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=15)
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None

        # Simulate wait completion: cancel the real wait task (instead of
        # orphaning it by nilling the reference) so no _wait_and_execute
        # task lingers after the test.
        sensor._state_machine._cancel_wait()
        assert sensor._wait_task is None

        hass.services.async_call.reset_mock()
        with patch(
            "homeassistant.util.dt.now", return_value=_now() + timedelta(minutes=15)
        ):
            # Call the completion callback directly
            await sensor._state_machine._complete_turn_off()

        assert sensor._state == STATE_IDLE
        hass.services.async_call.assert_awaited_once()


# =============================================================================
# Scenario 3: Demand Reversal During Wait
# =============================================================================


class TestScenario3DemandReversal:
    """Cases where demand changes back during wait period."""

    @pytest.mark.asyncio
    async def test_demand_off_during_pending_on(self, setup_sensor_hass):
        """Demand turns OFF while in PENDING_ON -> cancel wait, go to IDLE."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=10)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_PENDING_ON
        assert sensor._wait_task is not None

        # Demand turns OFF before wait completes
        await sensor.handle_demand_change(demand_on=False)

        # Wait should be cancelled
        assert sensor._wait_task is None
        assert sensor._wait_until is None
        assert sensor._state == STATE_IDLE
        assert sensor._demand_on is False

    @pytest.mark.asyncio
    async def test_demand_on_during_pending_off(self, setup_sensor_hass):
        """Demand turns ON while in PENDING_OFF -> cancel wait, go to HEATING."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=15)
        # Initial demand is ON
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None

        # Demand turns ON before wait completes
        await sensor.handle_demand_change(demand_on=True)

        # Wait should be cancelled
        assert sensor._wait_task is None
        assert sensor._wait_until is None
        assert sensor._state == STATE_HEATING
        assert sensor._demand_on is True


# =============================================================================
# Scenario 3b: Demand Changes At Timer Completion
# =============================================================================


class TestScenario3bDemandAtTimerCompletion:
    """Cases where demand changes at the exact moment wait timer completes."""

    @pytest.mark.asyncio
    async def test_pending_off_completes_but_demand_on(self, setup_sensor_hass):
        """PENDING_OFF wait completes,
        but demand changed to ON -> go to HEATING, no turn_off.
        """
        sensor, hass = setup_sensor_hass(min_on_min=30)

        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=15)
        # Initial demand is ON
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_PENDING_OFF

        # Simulate demand changing back to ON during the wait
        await sensor.handle_demand_change(demand_on=True)
        assert sensor._state == STATE_HEATING
        assert sensor._demand_on is True

        # Now simulate wait completion (even though demand is now ON)
        # The _complete_turn_off should check demand_on and not turn off
        with patch(
            "homeassistant.util.dt.now", return_value=_now() + timedelta(minutes=15)
        ):
            await sensor._state_machine._complete_turn_off()

        # Should NOT have called turn_off service
        # Should have set HEATING state
        assert sensor._state == STATE_HEATING
        # Count how many times turn_off was called (should be 0)
        turn_off_calls = [
            c
            for c in hass.services.async_call.await_args_list
            if len(c[0]) > 1 and c[0][1] == "turn_off"
        ]
        assert len(turn_off_calls) == 0

    @pytest.mark.asyncio
    async def test_pending_on_completes_but_demand_off(self, setup_sensor_hass):
        """PENDING_ON wait completes,
        but demand changed to OFF -> go to IDLE, no turn_on.
        """
        sensor, hass = setup_sensor_hass(min_off_min=25)

        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=10)
        # Initial demand is OFF
        sensor._demand_on = False

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_PENDING_ON

        # Simulate demand changing back to OFF during the wait
        await sensor.handle_demand_change(demand_on=False)
        assert sensor._state == STATE_IDLE
        assert sensor._demand_on is False

        # Now simulate wait completion (even though demand is now OFF)
        # The _complete_turn_on should check demand_on and not turn on
        with patch(
            "homeassistant.util.dt.now", return_value=_now() + timedelta(minutes=15)
        ):
            await sensor._state_machine._complete_turn_on()

        # Should NOT have called turn_on service
        # Should have set IDLE state
        assert sensor._state == STATE_IDLE
        # Count how many times turn_on was called (should be 0)
        turn_on_calls = [
            c
            for c in hass.services.async_call.await_args_list
            if len(c[0]) > 1 and c[0][1] == "turn_on"
        ]
        assert len(turn_on_calls) == 0


# =============================================================================
# Scenario 4: Boundary Conditions
# =============================================================================


class TestScenario4Boundaries:
    """Edge cases at exact duration boundaries."""

    @pytest.mark.asyncio
    async def test_turn_on_exactly_at_min_off_boundary(self, setup_sensor_hass):
        """Demand ON exactly at min_off_duration -> no wait, immediate HEATING."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        hass.states.is_state.return_value = False
        # Last off was exactly 25 minutes ago
        sensor._last_off = _now() - timedelta(minutes=25)
        # Initial demand is OFF
        sensor._demand_on = False

        hass.services.async_call.reset_mock()
        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_HEATING
        assert sensor._wait_task is None
        # Should have called turn_on immediately
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_turn_off_exactly_at_min_on_boundary(self, setup_sensor_hass):
        """Demand OFF exactly at min_on_duration -> no wait, immediate IDLE."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        hass.states.is_state.return_value = True
        # Last on was exactly 30 minutes ago
        sensor._last_on = _now() - timedelta(minutes=30)
        # Initial demand is ON
        sensor._demand_on = True

        hass.services.async_call.reset_mock()
        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_IDLE
        assert sensor._wait_task is None
        # Should have called turn_off immediately
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_turn_on_never_off_before(self, setup_sensor_hass):
        """First time turning ON, never was OFF -> requires full wait."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        hass.states.is_state.return_value = False
        sensor._last_off = None  # Never was off
        sensor._last_on = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        # Should require full min_off_duration wait
        assert sensor._state == STATE_PENDING_ON
        assert sensor._wait_task is not None

    @pytest.mark.asyncio
    async def test_turn_off_never_on_before(self, setup_sensor_hass):
        """First time turning OFF, never was ON -> requires full wait."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        hass.states.is_state.return_value = True
        sensor._last_on = None  # Never was on
        sensor._last_off = None
        # Initial demand is ON
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        # Should require full min_on_duration wait
        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None


# =============================================================================
# Scenario 5: Evaluate State Wait Scenarios
# =============================================================================


class TestScenario5EvaluateState:
    """Test _evaluate_state which also handles wait scenarios."""

    @pytest.mark.asyncio
    async def test_evaluate_state_triggers_pending_on(self, setup_sensor_hass):
        """_evaluate_state detects need for PENDING_ON."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        # Set up state: demand ON, relay OFF, recent last_off
        sensor._demand_on = True
        hass.states.is_state.return_value = False  # relay is OFF
        sensor._last_off = _now() - timedelta(minutes=10)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        assert sensor._state == STATE_PENDING_ON
        assert sensor._wait_task is not None

    @pytest.mark.asyncio
    async def test_evaluate_state_triggers_pending_off(self, setup_sensor_hass):
        """_evaluate_state detects need for PENDING_OFF."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        # Set up state: demand OFF, relay ON, recent last_on
        sensor._demand_on = False
        hass.states.get.return_value = MagicMock()
        hass.states.get.return_value.state = STATE_ON
        sensor._last_on = _now() - timedelta(minutes=15)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None

    @pytest.mark.asyncio
    async def test_evaluate_state_no_wait_needed(self, setup_sensor_hass):
        """_evaluate_state with no wait needed."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        # Set up state: demand OFF, relay ON, min_on elapsed
        sensor._demand_on = False
        hass.states.get.return_value = MagicMock()
        hass.states.get.return_value.state = STATE_ON
        sensor._last_on = _now() - timedelta(minutes=35)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._evaluate_state()

        assert sensor._state == STATE_IDLE
        assert sensor._wait_task is None


# =============================================================================
# Scenario 6: Compute Remaining Helper
# =============================================================================


class TestScenario6ComputeRemaining:
    """Test the _compute_remaining helper method."""

    def test_compute_remaining_with_none_last_time(self, make_sensor):
        """None last_time returns full duration."""
        sensor = make_sensor(min_off_min=25)
        result = sensor._compute_remaining(None, 25 * 60)
        assert result == 25 * 60  # Full duration in seconds

    def test_compute_remaining_elapsed_less_than_duration(self, make_sensor):
        """Elapsed time less than duration returns remaining."""
        sensor = make_sensor()
        now = _now()
        last_time = now - timedelta(minutes=10)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sensor._compute_remaining(last_time, 30 * 60)  # 30 min duration

        assert result == 20 * 60  # 20 minutes remaining

    def test_compute_remaining_elapsed_more_than_duration(self, make_sensor):
        """Elapsed time more than duration returns 0."""
        sensor = make_sensor()
        now = _now()
        last_time = now - timedelta(minutes=40)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sensor._compute_remaining(last_time, 30 * 60)  # 30 min duration

        assert result == 0

    def test_compute_remaining_exactly_at_duration(self, make_sensor):
        """Exactly at duration returns 0."""
        sensor = make_sensor()
        now = _now()
        last_time = now - timedelta(minutes=30)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sensor._compute_remaining(last_time, 30 * 60)

        assert result == 0


# =============================================================================
# Scenario 7: Rapid Demand Changes
# =============================================================================


class TestScenario7RapidChanges:
    """Test rapid demand toggling."""

    @pytest.mark.asyncio
    async def test_rapid_on_off_on_within_min_off(self, setup_sensor_hass):
        """Rapid ON->OFF->ON within min_off period."""
        sensor, hass = setup_sensor_hass(min_off_min=25)

        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=10)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            # First: turn ON
            await sensor.handle_demand_change(demand_on=True)

        assert sensor._state == STATE_PENDING_ON
        assert sensor._wait_task is not None

        # Second: turn OFF (before wait completes)
        await sensor.handle_demand_change(demand_on=False)
        assert sensor._state == STATE_IDLE
        assert sensor._wait_task is None

        # Third: turn ON again (still within min_off from original last_off)
        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=True)

        # Should start new wait
        assert sensor._state == STATE_PENDING_ON
        assert sensor._wait_task is not None

    @pytest.mark.asyncio
    async def test_rapid_off_on_off_within_min_on(self, setup_sensor_hass):
        """Rapid OFF->ON->OFF within min_on period."""
        sensor, hass = setup_sensor_hass(min_on_min=30)

        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=15)
        # Initial demand is ON
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            # First: turn OFF
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None

        # Second: turn ON (before wait completes)
        await sensor.handle_demand_change(demand_on=True)
        assert sensor._state == STATE_HEATING
        assert sensor._wait_task is None

        # Third: turn OFF again (still within min_on from original last_on)
        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        # Should start new wait
        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None


# =============================================================================
# Scenario 8: Additional Coverage Tests
# =============================================================================


class TestScenario8AdditionalCoverage:
    """Additional tests to improve coverage for edge cases."""

    @pytest.mark.asyncio
    async def test_handle_demand_change_no_change(self, setup_sensor_hass):
        """handle_demand_change called with same demand value -> early return."""
        sensor, _ = setup_sensor_hass()

        # Initial demand is False
        sensor._demand_on = False

        # Call with same value
        await sensor.handle_demand_change(demand_on=False)

        # Should return early, no state change
        assert sensor._demand_on is False
        # async_write_ha_state should not be called
        sensor.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_demand_calls_evaluate_state(
        self, setup_sensor_hass, monkeypatch
    ):
        """sync_demand updates demand and applies demand logic."""
        sensor, hass = setup_sensor_hass()

        sensor._demand_on = False
        # Mock the relay state to be OFF
        hass.states.is_state.return_value = False
        # Set last_off to be beyond min_off_duration so no wait is needed
        sensor._last_off = _now() - timedelta(minutes=30)

        await sensor.sync_demand(demand_on=True, demand_entity_id="switch.test")

        assert sensor._demand_on is True
        assert sensor._demand_entity_id == "switch.test"
        # Verify state was updated based on demand logic - should be HEATING
        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_sync_demand_without_entity_id(
        self, setup_sensor_hass, monkeypatch
    ):
        """sync_demand works without demand_entity_id."""
        sensor, hass = setup_sensor_hass()

        sensor._demand_on = False
        # Mock the relay state to be OFF
        hass.states.is_state.return_value = False
        # Set last_off to be beyond min_off_duration so no wait is needed
        sensor._last_off = _now() - timedelta(minutes=30)

        await sensor.sync_demand(demand_on=True)

        assert sensor._demand_on is True
        assert sensor._demand_entity_id is None
        # Verify state was updated based on demand logic - should be HEATING
        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_evaluate_state_relay_not_available(self, setup_sensor_hass):
        """_evaluate_state when relay entity is not yet available."""
        sensor, hass = setup_sensor_hass()

        # Mock relay_state to return None (not available)
        hass.states.get.return_value = None
        sensor._demand_on = True

        with patch("homeassistant.util.dt.now", return_value=_now()):
            with patch("stove_controller.state_machine._LOGGER") as mock_logger:
                await sensor._evaluate_state()

        # Should set to IDLE and log warning
        assert sensor._state == STATE_IDLE
        mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_relay_change_on_state(self, setup_sensor_hass):
        """_on_relay_change updates last_on when relay turns ON."""
        sensor, _ = setup_sensor_hass()

        # Set a previous last_off to verify it is preserved
        sensor._last_off = _now() - timedelta(minutes=10)

        # Create mock event with new_state = ON
        mock_event = MagicMock()
        mock_new_state = MagicMock()
        mock_new_state.state = STATE_ON
        mock_event.data = {"new_state": mock_new_state}

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(mock_event)

        assert sensor._last_on == _now()
        assert sensor._last_off == _now() - timedelta(minutes=10)

    @pytest.mark.asyncio
    async def test_on_relay_change_off_state(self, setup_sensor_hass):
        """_on_relay_change updates last_off when relay turns OFF."""
        sensor, _ = setup_sensor_hass()

        # Set a previous last_on to verify it is preserved
        sensor._last_on = _now() - timedelta(minutes=10)

        # Create mock event with new_state = OFF
        mock_event = MagicMock()
        mock_new_state = MagicMock()
        mock_new_state.state = STATE_OFF
        mock_event.data = {"new_state": mock_new_state}

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(mock_event)

        assert sensor._last_off == _now()
        assert sensor._last_on == _now() - timedelta(minutes=10)

    @pytest.mark.asyncio
    async def test_on_relay_change_no_new_state(self, setup_sensor_hass):
        """_on_relay_change returns early when new_state is None."""
        sensor, _ = setup_sensor_hass()

        # Create mock event with no new_state
        mock_event = MagicMock()
        mock_event.data = {"new_state": None}

        await sensor._on_relay_change(mock_event)

        # Should return early, no timestamps updated
        assert sensor._last_on is None
        assert sensor._last_off is None
