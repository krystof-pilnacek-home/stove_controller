"""Tests for the StoveStateMachine class."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)
from stove_controller.state_machine import StoveStateMachine


def _now() -> datetime:
    """Fixed timestamp for consistent testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def make_state_machine():
    """Factory fixture to create a StoveStateMachine."""
    def _make(
        hass: MagicMock | None = None,
        relay_entity: str = "switch.test_relay",
        min_on_min: int = 30,
        min_off_min: int = 25,
    ) -> StoveStateMachine:
        return StoveStateMachine(
            hass=hass,
            relay_entity=relay_entity,
            min_on_duration=min_on_min * 60,
            min_off_duration=min_off_min * 60,
        )
    return _make


@pytest.fixture
def setup_state_machine_hass(make_state_machine):
    """Factory fixture to set up a state machine with a mock hass."""
    def _setup(
        relay_entity: str = "switch.test_relay",
        min_on_min: int = 30,
        min_off_min: int = 25,
    ) -> tuple[StoveStateMachine, MagicMock]:
        hass = MagicMock()
        hass.states = MagicMock()
        hass.states.is_state = MagicMock(return_value=False)
        hass.states.get = MagicMock()
        hass.services = MagicMock()
        hass.services.async_call = AsyncMock()
        hass.async_create_task = MagicMock()

        sm = make_state_machine(hass, relay_entity, min_on_min, min_off_min)
        return sm, hass
    return _setup


# =============================================================================
# Initialization Tests
# =============================================================================


class TestInitialization:
    """Test state machine initialization."""

    def test_default_initialization(self, make_state_machine):
        """Test default initialization."""
        sm = make_state_machine()
        assert sm.state == STATE_IDLE
        assert sm.demand_on is False
        assert sm.last_on is None
        assert sm.last_off is None
        assert sm.wait_until is None
        assert sm.wait_task is None

    def test_initialization_with_hass(self, setup_state_machine_hass):
        """Test initialization with Home Assistant instance."""
        sm, _ = setup_state_machine_hass()
        assert sm.hass is not None
        assert sm.relay_entity == "switch.test_relay"

    def test_min_durations(self, make_state_machine):
        """Test min duration configuration."""
        sm = make_state_machine(min_on_min=45, min_off_min=30)
        assert sm.min_on_duration == 45 * 60
        assert sm.min_off_duration == 30 * 60


# =============================================================================
# Property Tests
# =============================================================================


class TestProperties:
    """Test state machine properties."""

    def test_is_in_grace_period_false_idle(self, make_state_machine):
        """IDLE state is not in grace period."""
        sm = make_state_machine()
        assert sm.is_in_grace_period is False

    def test_is_in_grace_period_false_heating(self, make_state_machine):
        """HEATING state is not in grace period."""
        sm = make_state_machine()
        sm._state = STATE_HEATING
        assert sm.is_in_grace_period is False

    def test_is_in_grace_period_true_pending_on(self, make_state_machine):
        """PENDING_ON state is in grace period."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_ON
        assert sm.is_in_grace_period is True

    def test_is_in_grace_period_true_pending_off(self, make_state_machine):
        """PENDING_OFF state is in grace period."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_OFF
        assert sm.is_in_grace_period is True

    def test_get_remaining_seconds_no_wait(self, make_state_machine):
        """No wait in progress returns 0."""
        sm = make_state_machine()
        assert sm.get_remaining_seconds() == 0

    def test_get_remaining_seconds_with_wait(self, make_state_machine):
        """Wait in progress returns remaining seconds."""
        sm = make_state_machine()
        sm._wait_until = _now() + timedelta(seconds=100)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            result = sm.get_remaining_seconds()

        assert result == 100


# =============================================================================
# Transition Validation Tests
# =============================================================================


class TestTransitionValidation:
    """Test state transition validation."""

    def test_valid_transition_idle_to_heating(self, make_state_machine):
        """IDLE -> HEATING is valid."""
        sm = make_state_machine()
        assert sm.is_valid_transition(STATE_HEATING) is True

    def test_valid_transition_idle_to_pending_on(self, make_state_machine):
        """IDLE -> PENDING_ON is valid."""
        sm = make_state_machine()
        assert sm.is_valid_transition(STATE_PENDING_ON) is True

    def test_valid_transition_idle_to_pending_off(self, make_state_machine):
        """IDLE -> PENDING_OFF is valid (relay ON but demand OFF)."""
        sm = make_state_machine()
        assert sm.is_valid_transition(STATE_PENDING_OFF) is True

    def test_valid_transition_heating_to_idle(self, make_state_machine):
        """HEATING -> IDLE is valid."""
        sm = make_state_machine()
        sm._state = STATE_HEATING
        assert sm.is_valid_transition(STATE_IDLE) is True

    def test_valid_transition_heating_to_pending_off(self, make_state_machine):
        """HEATING -> PENDING_OFF is valid."""
        sm = make_state_machine()
        sm._state = STATE_HEATING
        assert sm.is_valid_transition(STATE_PENDING_OFF) is True

    def test_valid_transition_heating_to_pending_on(self, make_state_machine):
        """HEATING -> PENDING_ON is valid (relay OFF but demand ON)."""
        sm = make_state_machine()
        sm._state = STATE_HEATING
        assert sm.is_valid_transition(STATE_PENDING_ON) is True

    def test_valid_transition_pending_on_to_heating(self, make_state_machine):
        """PENDING_ON -> HEATING is valid."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_ON
        assert sm.is_valid_transition(STATE_HEATING) is True

    def test_valid_transition_pending_on_to_idle(self, make_state_machine):
        """PENDING_ON -> IDLE is valid."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_ON
        assert sm.is_valid_transition(STATE_IDLE) is True

    def test_valid_transition_pending_off_to_idle(self, make_state_machine):
        """PENDING_OFF -> IDLE is valid."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_OFF
        assert sm.is_valid_transition(STATE_IDLE) is True

    def test_valid_transition_pending_off_to_heating(self, make_state_machine):
        """PENDING_OFF -> HEATING is valid."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_OFF
        assert sm.is_valid_transition(STATE_HEATING) is True


# =============================================================================
# State Restoration Tests
# =============================================================================


class TestStateRestoration:
    """Test state restoration from saved data."""

    def test_restore_valid_state(self, make_state_machine):
        """Restore a valid state."""
        sm = make_state_machine()
        now = _now()

        sm.restore_state(
            state=STATE_HEATING,
            demand_on=True,
            last_on=now,
            last_off=now - timedelta(minutes=30),
        )

        assert sm.state == STATE_HEATING
        assert sm.demand_on is True
        assert sm.last_on == now
        assert sm.last_off == now - timedelta(minutes=30)

    def test_restore_invalid_state(self, make_state_machine):
        """Restore an invalid state defaults to IDLE."""
        sm = make_state_machine()

        sm.restore_state(state="invalid_state")

        assert sm.state == STATE_IDLE

    def test_restore_state_string(self, make_state_machine):
        """Restore state from string."""
        sm = make_state_machine()

        sm.restore_state(state="heating")

        assert sm.state == STATE_HEATING


# =============================================================================
# Compute Remaining Tests
# =============================================================================


class TestComputeRemaining:
    """Test _compute_remaining helper."""

    def test_none_last_time_returns_full_duration(self, make_state_machine):
        """None last_time returns full duration."""
        sm = make_state_machine()
        result = sm._compute_remaining(None, 100)
        assert result == 100

    def test_elapsed_less_than_duration(self, make_state_machine):
        """Elapsed time less than duration returns remaining."""
        sm = make_state_machine()
        now = _now()
        last_time = now - timedelta(seconds=50)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sm._compute_remaining(last_time, 100)

        assert result == 50

    def test_elapsed_more_than_duration(self, make_state_machine):
        """Elapsed time more than duration returns 0."""
        sm = make_state_machine()
        now = _now()
        last_time = now - timedelta(seconds=150)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sm._compute_remaining(last_time, 100)

        assert result == 0

    def test_exactly_at_duration(self, make_state_machine):
        """Exactly at duration returns 0."""
        sm = make_state_machine()
        now = _now()
        last_time = now - timedelta(seconds=100)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sm._compute_remaining(last_time, 100)

        assert result == 0


# =============================================================================
# Demand Logic Tests (Async)
# =============================================================================


class TestDemandLogic:
    """Test demand-driven state transitions."""

    @pytest.mark.asyncio
    async def test_set_demand_on_no_wait(self, setup_state_machine_hass):
        """Demand ON with no wait needed -> HEATING."""
        sm, hass = setup_state_machine_hass(min_off_min=25)

        # Relay is OFF
        hass.states.is_state.return_value = False
        # Last off was 30 minutes ago (> 25 min)
        sm._last_off = _now() - timedelta(minutes=30)
        sm._last_on = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sm.set_demand(demand_on=True)

        assert sm.demand_on is True
        assert sm.state == STATE_HEATING
        # turn_on should have been called
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_demand_on_with_wait(self, setup_state_machine_hass):
        """Demand ON with wait needed -> PENDING_ON."""
        sm, hass = setup_state_machine_hass(min_off_min=25)

        # Relay is OFF
        hass.states.is_state.return_value = False
        # Last off was only 10 minutes ago (< 25 min)
        sm._last_off = _now() - timedelta(minutes=10)
        sm._last_on = None

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sm.set_demand(demand_on=True)

        assert sm.demand_on is True
        assert sm.state == STATE_PENDING_ON
        assert sm.wait_task is not None
        assert sm.wait_until is not None
        # turn_on should NOT have been called yet
        hass.services.async_call.assert_not_awaited()
        # The wait timer must run as an untracked asyncio task (not via
        # hass.async_create_task) so hass.async_block_till_done() does not
        # block on the long sleep.
        hass.async_create_task.assert_not_called()
        sm.cancel_wait()

    @pytest.mark.asyncio
    async def test_set_demand_off_with_wait(self, setup_state_machine_hass):
        """Demand OFF with wait needed -> PENDING_OFF."""
        sm, hass = setup_state_machine_hass(min_on_min=30)

        # Relay is ON
        hass.states.is_state.return_value = True
        # Last on was only 15 minutes ago (< 30 min)
        sm._last_on = _now() - timedelta(minutes=15)
        sm._last_off = None
        # Initial demand is ON
        sm._demand_on = True
        # Set initial state to HEATING (demand ON, relay ON)
        await sm.transition_to(STATE_HEATING)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sm.set_demand(demand_on=False)

        assert sm.demand_on is False
        assert sm.state == STATE_PENDING_OFF
        assert sm.wait_task is not None
        # turn_off should NOT have been called yet
        hass.services.async_call.assert_not_awaited()
        hass.async_create_task.assert_not_called()
        sm.cancel_wait()

    @pytest.mark.asyncio
    async def test_demand_unchanged_no_op(self, setup_state_machine_hass):
        """Setting same demand value does nothing."""
        sm, hass = setup_state_machine_hass()
        sm._demand_on = True

        await sm.set_demand(demand_on=True)

        assert sm.demand_on is True
        # No state change, no service calls
        hass.services.async_call.assert_not_awaited()
        sm.cancel_wait()


# =============================================================================
# Wait Management Tests
# =============================================================================


class TestWaitManagement:
    """Test wait timer management."""

    def test_cancel_wait_clears_task(self, make_state_machine):
        """Cancel wait clears task and wait_until."""
        sm = make_state_machine()
        mock_task = MagicMock()
        mock_task.done.return_value = False

        sm._wait_task = mock_task
        sm._wait_until = _now() + timedelta(seconds=100)

        sm._cancel_wait()

        mock_task.cancel.assert_called_once()
        assert sm.wait_task is None
        assert sm.wait_until is None

    def test_cancel_wait_skips_done_task(self, make_state_machine):
        """Cancel wait doesn't cancel already done tasks."""
        sm = make_state_machine()
        mock_task = MagicMock()
        mock_task.done.return_value = True

        sm._wait_task = mock_task
        sm._wait_until = _now() + timedelta(seconds=100)

        sm._cancel_wait()

        mock_task.cancel.assert_not_called()
        assert sm.wait_task is None
        assert sm.wait_until is None

    @pytest.mark.asyncio
    async def test_start_wait_zero_duration(self, make_state_machine):
        """Start wait with zero duration executes immediately."""
        sm = make_state_machine()

        callback = AsyncMock()
        sm._start_wait(0, callback)

        # Should execute immediately via asyncio.create_task
        # The callback should have been scheduled
        # We can't easily await it here, but we can verify it was called
        # by checking that the task was created
        assert sm.wait_task is None  # Zero duration doesn't set wait_task

    @pytest.mark.asyncio
    async def test_start_wait_positive_duration(self, setup_state_machine_hass):
        """Start wait with positive duration sets wait_until."""
        sm, hass = setup_state_machine_hass()

        callback = AsyncMock()
        with patch("homeassistant.util.dt.now", return_value=_now()):
            sm._start_wait(100, callback)

        assert sm.wait_until == _now() + timedelta(seconds=100)
        assert sm.wait_task is not None
        # The wait timer runs as an untracked asyncio task (not via
        # hass.async_create_task) so hass.async_block_till_done() does not
        # block on the long sleep.
        hass.async_create_task.assert_not_called()
        sm.cancel_wait()


# =============================================================================
# Callback Tests
# =============================================================================


class TestCallbacks:
    """Test state change callbacks."""

    @pytest.mark.asyncio
    async def test_state_change_callback(self, make_state_machine):
        """State change triggers callback."""
        sm = make_state_machine()
        callback = AsyncMock()

        sm.register_on_state_change(callback)

        await sm.transition_to(STATE_HEATING)

        callback.assert_awaited_once_with(STATE_HEATING)

    @pytest.mark.asyncio
    async def test_state_change_callback_error(self, make_state_machine):
        """Callback error doesn't prevent state change."""
        sm = make_state_machine()

        async def bad_callback(state):
            raise ValueError("Test error")

        sm.register_on_state_change(bad_callback)

        # Should still transition despite callback error
        result = await sm.transition_to(STATE_HEATING)

        assert result is True
        assert sm.state == STATE_HEATING


# =============================================================================
# Transition Tests
# =============================================================================


class TestTransitions:
    """Test state transitions."""

    @pytest.mark.asyncio
    async def test_valid_transition_succeeds(self, make_state_machine):
        """Valid transition succeeds."""
        sm = make_state_machine()
        result = await sm.transition_to(STATE_HEATING)
        assert result is True
        assert sm.state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_invalid_transition_fails(self, make_state_machine):
        """Invalid transition fails."""
        sm = make_state_machine()
        # Find an actually invalid transition - IDLE -> IDLE should fail
        result = await sm.transition_to(STATE_IDLE)
        assert result is False
        assert sm.state == STATE_IDLE
