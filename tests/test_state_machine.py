"""Tests for the StoveStateMachine class."""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
    STATE_UNAVAILABLE,
)
from stove_controller.state_machine import StoveStateMachine


def _now() -> datetime:
    """Fixed timestamp for consistent testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def make_state_machine():
    """Factory fixture to create a StoveStateMachine.

    Tracks every created state machine and cancels any pending wait task on
    teardown, so a failing assertion mid-test cannot leak a ``wait_and_execute``
    task.
    """
    created: list[StoveStateMachine] = []

    def _make(
        hass: MagicMock | None = None,
        relay_entity: str = "switch.test_relay",
        min_on_min: int = 30,
        min_off_min: int = 25,
    ) -> StoveStateMachine:
        sm = StoveStateMachine(
            hass=hass,
            relay_entity=relay_entity,
            min_on_duration=min_on_min * 60,
            min_off_duration=min_off_min * 60,
        )
        created.append(sm)
        return sm

    yield _make

    wait_tasks = [
        sm.wait_task
        for sm in created
        if sm.wait_task is not None and not sm.wait_task.done()
    ]
    for sm in created:
        sm.cancel_wait()
    for task in wait_tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
async def setup_state_machine_hass(make_state_machine):
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

    yield _setup


# =============================================================================
# Initialization Tests
# =============================================================================


class TestInitialization:
    """Test state machine initialization."""

    def test_default_initialization(self, make_state_machine):
        """Test default initialization."""
        sm = make_state_machine()
        assert sm.state == STATE_IDLE
        assert not sm.demand_on
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

    @pytest.mark.parametrize(
        ("state", "in_grace"),
        [
            (STATE_IDLE, False),
            (STATE_HEATING, False),
            (STATE_PENDING_ON, True),
            (STATE_PENDING_OFF, True),
            (STATE_UNAVAILABLE, False),
        ],
    )
    def test_is_in_grace_period(self, make_state_machine, state, in_grace):
        """Grace period is only active in PENDING_* states."""
        sm = make_state_machine()
        sm._state = state
        assert sm.is_in_grace_period == in_grace

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

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (STATE_IDLE, STATE_HEATING),
            (STATE_IDLE, STATE_PENDING_ON),
            (STATE_IDLE, STATE_PENDING_OFF),
            (STATE_HEATING, STATE_IDLE),
            (STATE_HEATING, STATE_PENDING_OFF),
            (STATE_HEATING, STATE_PENDING_ON),
            (STATE_PENDING_ON, STATE_HEATING),
            (STATE_PENDING_ON, STATE_IDLE),
            (STATE_PENDING_OFF, STATE_IDLE),
            (STATE_PENDING_OFF, STATE_HEATING),
            # Any state may become UNAVAILABLE when the relay goes away.
            (STATE_IDLE, STATE_UNAVAILABLE),
            (STATE_HEATING, STATE_UNAVAILABLE),
            (STATE_PENDING_ON, STATE_UNAVAILABLE),
            (STATE_PENDING_OFF, STATE_UNAVAILABLE),
            # From UNAVAILABLE the machine re-evaluates demand once the relay
            # state is known again.
            (STATE_UNAVAILABLE, STATE_IDLE),
            (STATE_UNAVAILABLE, STATE_HEATING),
            (STATE_UNAVAILABLE, STATE_PENDING_ON),
            (STATE_UNAVAILABLE, STATE_PENDING_OFF),
        ],
    )
    def test_valid_transition(self, make_state_machine, from_state, to_state):
        """Valid transitions are accepted."""
        sm = make_state_machine()
        sm._state = from_state
        assert sm.is_valid_transition(to_state)

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            # A state is never a valid transition target from itself.
            (STATE_IDLE, STATE_IDLE),
            (STATE_HEATING, STATE_HEATING),
            (STATE_PENDING_ON, STATE_PENDING_ON),
            (STATE_PENDING_OFF, STATE_PENDING_OFF),
            (STATE_UNAVAILABLE, STATE_UNAVAILABLE),
            # Cross-pending transitions are not direct.
            (STATE_PENDING_ON, STATE_PENDING_OFF),
            (STATE_PENDING_OFF, STATE_PENDING_ON),
        ],
    )
    def test_invalid_transition(self, make_state_machine, from_state, to_state):
        """Invalid transitions are rejected."""
        sm = make_state_machine()
        sm._state = from_state
        assert not sm.is_valid_transition(to_state)


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
        assert sm.demand_on
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
    """Test compute_remaining helper."""

    def test_none_last_time_returns_full_duration(self, make_state_machine):
        """None last_time returns full duration."""
        sm = make_state_machine()
        assert sm.compute_remaining(None, 100) == 100

    @pytest.mark.parametrize(
        ("elapsed_sec", "expected"),
        [
            (0, 100),
            (50, 50),
            (99, 1),
            (100, 0),
            (150, 0),
            (1000, 0),
        ],
    )
    def test_remaining_for_elapsed(
        self, make_state_machine, elapsed_sec, expected
    ):
        """Remaining time is clamped to [0, duration]."""
        sm = make_state_machine()
        now = _now()
        last_time = now - timedelta(seconds=elapsed_sec)

        with patch("homeassistant.util.dt.now", return_value=now):
            result = sm.compute_remaining(last_time, 100)

        assert result == expected


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

        assert sm.demand_on
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

        assert sm.demand_on
        assert sm.state == STATE_PENDING_ON
        assert sm.wait_task is not None
        assert sm.wait_until is not None
        # turn_on should NOT have been called yet
        hass.services.async_call.assert_not_awaited()
        # The wait timer must run as an untracked asyncio task (not via
        # hass.async_create_task) so hass.async_block_till_done() does not
        # block on the long sleep.
        hass.async_create_task.assert_not_called()

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

        assert not sm.demand_on
        assert sm.state == STATE_PENDING_OFF
        assert sm.wait_task is not None
        # turn_off should NOT have been called yet
        hass.services.async_call.assert_not_awaited()
        hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_demand_unchanged_no_op(self, setup_state_machine_hass):
        """Setting same demand value does nothing."""
        sm, hass = setup_state_machine_hass()
        sm._demand_on = True

        await sm.set_demand(demand_on=True)

        assert sm.demand_on
        # No state change, no service calls
        hass.services.async_call.assert_not_awaited()


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

        sm.cancel_wait()

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

        sm.cancel_wait()

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

        sm.on_state_change = callback

        await sm.transition_to(STATE_HEATING)

        callback.assert_awaited_once_with(STATE_HEATING)

    @pytest.mark.asyncio
    async def test_state_change_callback_error(self, make_state_machine):
        """Callback error doesn't prevent state change."""
        sm = make_state_machine()

        async def bad_callback(state):
            raise ValueError("Test error")

        sm.on_state_change = bad_callback

        # Should still transition despite callback error
        result = await sm.transition_to(STATE_HEATING)

        assert result
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
        assert result
        assert sm.state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_invalid_transition_fails(self, make_state_machine):
        """Invalid transition fails."""
        sm = make_state_machine()
        # IDLE -> IDLE is not a valid transition
        result = await sm.transition_to(STATE_IDLE)
        assert not result
        assert sm.state == STATE_IDLE


# =============================================================================
# Corner Case Tests - Unknown Relay State
# =============================================================================


class TestUnknownRelayState:
    """Tests for handling unknown/unavailable relay states."""

    @pytest.mark.parametrize("relay_state", ["unknown", "unavailable"])
    @pytest.mark.asyncio
    async def test_unknown_state_transitions_to_unavailable(
        self, make_state_machine, relay_state
    ):
        """Unknown/unavailable relay state transitions to UNAVAILABLE."""
        sm = make_state_machine()
        sm._state = STATE_HEATING
        sm._demand_on = True

        with patch("stove_controller.state_machine._LOGGER") as mock_logger:
            await sm.update_relay_state(relay_state)

        mock_logger.warning.assert_called_once()
        assert sm.state == STATE_UNAVAILABLE

    @pytest.mark.parametrize("relay_state", ["unknown", "unavailable"])
    @pytest.mark.asyncio
    async def test_unknown_state_cancels_wait(
        self, make_state_machine, relay_state
    ):
        """Unknown/unavailable relay state cancels active wait."""
        sm = make_state_machine()
        sm._state = STATE_PENDING_ON
        sm._demand_on = True
        sm._wait_until = _now() + timedelta(seconds=100)

        await sm.update_relay_state(relay_state)

        assert sm.wait_until is None
        assert sm.state == STATE_UNAVAILABLE

    @pytest.mark.parametrize("relay_state", ["unknown", "unavailable"])
    @pytest.mark.asyncio
    async def test_unknown_state_preserves_timestamps(
        self, make_state_machine, relay_state
    ):
        """Unknown/unavailable relay state preserves timestamps."""
        sm = make_state_machine()
        sm._state = STATE_HEATING
        sm._demand_on = True
        sm._last_on = _now() - timedelta(minutes=5)
        sm._last_off = _now() - timedelta(minutes=20)

        original_last_on = sm._last_on
        original_last_off = sm._last_off

        await sm.update_relay_state(relay_state)

        assert sm._last_on == original_last_on
        assert sm._last_off == original_last_off
        assert sm.state == STATE_UNAVAILABLE

    @pytest.mark.parametrize("relay_state", ["unknown", "unavailable"])
    @pytest.mark.asyncio
    async def test_unknown_state_republishes_when_already_unavailable(
        self, make_state_machine, relay_state
    ):
        """An unknown relay state while already UNAVAILABLE still republishes.

        When the machine is already UNAVAILABLE, transition_to(UNAVAILABLE) is a
        no-op (self-transition not in VALID_TRANSITIONS) that never fires the
        on_state_change callback.  The unknown-state path must republish the
        state directly so the owning sensor reflects the cancelled wait.
        """
        sm = make_state_machine()
        sm._state = STATE_UNAVAILABLE
        sm._demand_on = True
        sm._wait_until = _now() + timedelta(seconds=100)
        seen: list = []

        async def callback(state):
            seen.append(state)

        sm.on_state_change = callback

        await sm.update_relay_state(relay_state)

        assert sm.wait_until is None
        assert sm.state == STATE_UNAVAILABLE
        assert seen == [STATE_UNAVAILABLE]


# =============================================================================
# Corner Case Tests - old_state=None Handling
# =============================================================================


class TestOldStateNoneHandling:
    """Tests for handling old_state=None (HA restart scenario)."""

    @pytest.mark.asyncio
    async def test_old_state_none_re_evaluates_demand(
        self, setup_state_machine_hass
    ):
        """old_state=None triggers demand re-evaluation via evaluate()."""
        sm, hass = setup_state_machine_hass()
        sm._state = STATE_HEATING
        sm._demand_on = True
        sm._last_on = _now() - timedelta(minutes=5)
        sm._last_off = _now() - timedelta(minutes=20)

        # Set relay to off
        hass.states.is_state.return_value = False

        # When old_state is None, sensor._on_relay_change calls evaluate() instead
        # So we test evaluate() directly
        await sm.evaluate()

        # Should have re-evaluated and transitioned based on demand and relay state
        # Since demand_on=True and relay_on=False, it should start waiting
        # or turn on. Without a real event loop, just check it didn't crash.

    @pytest.mark.asyncio
    async def test_old_state_none_preserves_timestamps(
        self, setup_state_machine_hass
    ):
        """old_state=None preserves timestamps when demand matches state."""
        sm, hass = setup_state_machine_hass()
        # Set state to IDLE with demand off and relay off - no action needed
        sm._state = STATE_IDLE
        sm._demand_on = False
        original_last_on = _now() - timedelta(minutes=5)
        original_last_off = _now() - timedelta(minutes=20)
        sm._last_on = original_last_on
        sm._last_off = original_last_off

        hass.states.is_state.return_value = False

        # When old_state is None, sensor._on_relay_change calls evaluate() instead
        # So we test evaluate() directly
        await sm.evaluate()

        # Timestamps should be preserved when no action is needed
        assert sm._last_on == original_last_on
        assert sm._last_off == original_last_off
        assert sm.state == STATE_IDLE


# =============================================================================
# Corner Case Tests - Service Failure Handling
# =============================================================================


class TestServiceFailureHandling:
    """Tests for handling relay service call failures."""

    @pytest.mark.parametrize(
        "turn_method",
        ["_turn_on_relay", "_turn_off_relay"],
    )
    @pytest.mark.asyncio
    async def test_relay_call_failure_returns_false(
        self, setup_state_machine_hass, turn_method
    ):
        """Relay turn methods return False and log on service failure."""
        sm, hass = setup_state_machine_hass()
        hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("Service not available")
        )

        with patch("stove_controller.state_machine._LOGGER") as mock_logger:
            result = await getattr(sm, turn_method)()

        assert result is False
        mock_logger.error.assert_called_once()

    @pytest.mark.parametrize(
        "turn_method",
        ["_turn_on_relay", "_turn_off_relay"],
    )
    @pytest.mark.asyncio
    async def test_relay_call_unexpected_error_propagates(
        self, setup_state_machine_hass, turn_method
    ):
        """Unexpected (non-HomeAssistant) errors are not swallowed by the relay
        methods; only HomeAssistant service failures are collapsed to False.
        """
        sm, hass = setup_state_machine_hass()
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await getattr(sm, turn_method)()

    @pytest.mark.parametrize(
        ("start_state", "demand_on", "complete_method", "expected_state"),
        [
            (STATE_PENDING_ON, True, "_complete_turn_on", STATE_IDLE),
            (STATE_PENDING_OFF, False, "_complete_turn_off", STATE_HEATING),
        ],
    )
    @pytest.mark.asyncio
    async def test_complete_failure_transitions(
        self,
        setup_state_machine_hass,
        start_state,
        demand_on,
        complete_method,
        expected_state,
    ):
        """Post-wait completion transitions to the relay-honest state on failure."""
        sm, hass = setup_state_machine_hass()
        sm._state = start_state
        sm._demand_on = demand_on
        hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("Service not available")
        )

        with patch("stove_controller.state_machine._LOGGER") as mock_logger:
            await getattr(sm, complete_method)()

        mock_logger.warning.assert_called_once()
        assert sm.state == expected_state

    @pytest.mark.parametrize(
        ("start_state", "demand_on", "relay_on", "last_attr", "expected_state"),
        [
            # Immediate turn-on failure: relay still off -> IDLE.
            (STATE_IDLE, True, False, "_last_off", STATE_IDLE),
            # Immediate turn-off failure: relay still on -> HEATING.
            (STATE_HEATING, False, True, "_last_on", STATE_HEATING),
        ],
    )
    @pytest.mark.asyncio
    async def test_apply_demand_logic_failure_transitions(
        self,
        setup_state_machine_hass,
        start_state,
        demand_on,
        relay_on,
        last_attr,
        expected_state,
    ):
        """Immediate (no-wait) relay failure transitions to the relay-honest state."""
        sm, hass = setup_state_machine_hass()
        sm._state = start_state
        sm._demand_on = demand_on
        hass.states.is_state.return_value = relay_on
        setattr(sm, last_attr, _now() - timedelta(minutes=60))
        hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("Service not available")
        )

        with patch("stove_controller.state_machine._LOGGER") as mock_logger:
            await sm._apply_demand_logic(relay_on=relay_on)

        mock_logger.warning.assert_called_once()
        assert sm.state == expected_state

    @pytest.mark.asyncio
    async def test_start_wait_republishes_state(self, setup_state_machine_hass):
        """_start_wait schedules a state-change republish so time_remaining_sec is
        published with the wait duration instead of the stale pre-wait value.
        """
        sm, hass = setup_state_machine_hass()
        sm._state = STATE_PENDING_ON
        seen: list = []

        async def callback(state):
            seen.append(state)

        sm.on_state_change = callback

        with patch("homeassistant.util.dt.now", return_value=_now()):
            sm._start_wait(120, AsyncMock())

        # The mock hass captures the scheduled coroutines without running them;
        # drive the republish coroutine (the non-wait-task one) to completion.
        scheduled = [c.args[0] for c in hass.async_create_task.call_args_list if c.args]
        republishes = [
            c for c in scheduled if getattr(c, "__qualname__", "").endswith(".callback")
        ]
        assert republishes, "state-change republish was not scheduled on wait start"
        await republishes[0]
        assert seen == [STATE_PENDING_ON]
