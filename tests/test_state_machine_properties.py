"""Property-based tests for the StoveStateMachine pure logic.

These supplement the example-based coverage in ``test_state_machine.py`` with
Hypothesis properties over the parts of ``state_machine.py`` whose contracts are
property-shaped rather than example-shaped:

- ``compute_remaining`` clamping and monotonicity over arbitrary
  ``(last_time, min_duration)`` pairs, including fractional-second inputs that
  exercise the ``int()`` truncation the hand-picked integer list can miss.
- ``is_valid_transition`` membership matching the ``VALID_TRANSITIONS`` table
  over arbitrary ``(from, to)`` state pairs, including the "no self-transition"
  rule.
- ``restore_state`` consistency correction: after restoring any state, the
  resulting ``demand_on`` is always consistent with ``state`` per the
  documented pairing.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from stove_controller.const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
    STATE_UNAVAILABLE,
    ControllerState,
)
from stove_controller.state_machine import StoveStateMachine


def _now() -> datetime:
    """Fixed timestamp for consistent property testing."""
    return datetime(2026, 1, 15, 20, 0, 0, tzinfo=UTC)


def _make_state_machine() -> StoveStateMachine:
    """Create a StoveStateMachine without Home Assistant for pure-logic tests."""
    return StoveStateMachine(
        hass=None,
        relay_entity="switch.test_relay",
        min_on_duration=1800,
        min_off_duration=1500,
    )


# Documented (state -> demand_on) pairing enforced by restore_state.
CONSISTENT_DEMAND: dict[ControllerState, bool] = {
    STATE_HEATING: True,
    STATE_IDLE: False,
    STATE_PENDING_ON: True,
    STATE_PENDING_OFF: False,
}

ALL_STATES: list[ControllerState] = [
    STATE_IDLE,
    STATE_HEATING,
    STATE_PENDING_ON,
    STATE_PENDING_OFF,
    STATE_UNAVAILABLE,
]


# Strategies -----------------------------------------------------------------

# Non-negative elapsed seconds, including fractional values so the int()
# truncation in compute_remaining is exercised at sub-second boundaries.
ELAPSED_SEC = st.floats(
    min_value=0.0,
    max_value=1_000_000.0,
    allow_nan=False,
    allow_infinity=False,
)
MIN_DURATION = st.integers(min_value=0, max_value=1_000_000)
STATE = st.sampled_from(ALL_STATES)
ACTIVE_STATE = st.sampled_from(list(CONSISTENT_DEMAND))


# =============================================================================
# compute_remaining clamping and monotonicity
# =============================================================================


class TestComputeRemainingProperties:
    """Property-based tests for compute_remaining."""

    @given(elapsed=ELAPSED_SEC, min_duration=MIN_DURATION)
    @settings(max_examples=100, deadline=None)
    def test_remaining_is_clamped_to_zero_and_duration(
        self, elapsed: float, min_duration: int
    ):
        """For elapsed >= 0, result is clamped to [0, min_duration]."""
        sm = _make_state_machine()
        last_time = _now() - timedelta(seconds=elapsed)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            result = sm.compute_remaining(last_time, min_duration)

        assert 0 <= result <= min_duration

    @given(
        elapsed_a=ELAPSED_SEC,
        elapsed_b=ELAPSED_SEC,
        min_duration=MIN_DURATION,
    )
    @settings(max_examples=100, deadline=None)
    def test_remaining_is_monotonic_in_elapsed(
        self, elapsed_a: float, elapsed_b: float, min_duration: int
    ):
        """Remaining time is non-increasing as elapsed time increases."""
        sm = _make_state_machine()
        now = _now()
        last_time_a = now - timedelta(seconds=elapsed_a)
        last_time_b = now - timedelta(seconds=elapsed_b)

        with patch("homeassistant.util.dt.now", return_value=now):
            result_a = sm.compute_remaining(last_time_a, min_duration)
            result_b = sm.compute_remaining(last_time_b, min_duration)

        if elapsed_a <= elapsed_b:
            assert result_a >= result_b
        else:
            assert result_a <= result_b

    @given(min_duration=MIN_DURATION)
    @settings(max_examples=100, deadline=None)
    def test_none_last_time_returns_full_duration(self, min_duration: int):
        """A None last_time yields the full min_duration."""
        sm = _make_state_machine()
        assert sm.compute_remaining(None, min_duration) == min_duration


# =============================================================================
# is_valid_transition matches the VALID_TRANSITIONS table
# =============================================================================


class TestValidTransitionProperties:
    """Property-based tests for is_valid_transition / VALID_TRANSITIONS."""

    @given(from_state=STATE, to_state=STATE)
    @settings(max_examples=100, deadline=None)
    def test_membership_matches_table(
        self, from_state: ControllerState, to_state: ControllerState
    ):
        """is_valid_transition(to) is exactly table membership for the pair."""
        sm = _make_state_machine()
        sm._state = from_state

        expected = to_state in StoveStateMachine.VALID_TRANSITIONS[from_state]

        assert sm.is_valid_transition(to_state) is expected

    @given(state=STATE)
    @settings(max_examples=100, deadline=None)
    def test_no_self_transition(self, state: ControllerState):
        """No state is a valid transition target from itself."""
        sm = _make_state_machine()
        sm._state = state

        assert state not in StoveStateMachine.VALID_TRANSITIONS[state]
        assert sm.is_valid_transition(state) is False

    @given(state=STATE)
    @settings(max_examples=100, deadline=None)
    def test_table_has_no_self_entry(self, state: ControllerState):
        """The VALID_TRANSITIONS table never lists a state as its own target."""
        assert state not in StoveStateMachine.VALID_TRANSITIONS[state]


# =============================================================================
# restore_state (state, demand_on) consistency invariant
# =============================================================================


class TestRestoreStateConsistency:
    """Property-based tests for the restore_state consistency correction."""

    @given(state=ACTIVE_STATE, demand_on=st.booleans())
    @settings(max_examples=100, deadline=None)
    def test_demand_matches_state_after_restore(
        self, state: ControllerState, demand_on: bool
    ):
        """After restore_state, demand_on is consistent with the active state.

        restore_state corrects demand_on to match the documented pairing for
        HEATING/IDLE/PENDING_ON/PENDING_OFF regardless of the supplied value.
        wait_until is left as None so no wait task is scheduled (keeps the
        property synchronous and side-effect free).
        """
        sm = _make_state_machine()

        sm.restore_state(state=state, demand_on=demand_on)

        assert sm.state == state
        assert sm.demand_on is CONSISTENT_DEMAND[state]

    @given(state=ACTIVE_STATE, demand_on=st.booleans())
    @settings(max_examples=100, deadline=None)
    def test_restore_preserves_timestamps(
        self, state: ControllerState, demand_on: bool
    ):
        """restore_state stores last_on/last_off verbatim."""
        sm = _make_state_machine()
        last_on = _now() - timedelta(minutes=5)
        last_off = _now() - timedelta(minutes=20)

        sm.restore_state(
            state=state,
            demand_on=demand_on,
            last_on=last_on,
            last_off=last_off,
        )

        assert sm.last_on == last_on
        assert sm.last_off == last_off

    @given(demand_on=st.booleans())
    @settings(max_examples=100, deadline=None)
    def test_unavailable_preserves_demand(self, demand_on: bool):
        """UNAVAILABLE has no consistency rule, so demand_on is preserved."""
        sm = _make_state_machine()

        sm.restore_state(state=STATE_UNAVAILABLE, demand_on=demand_on)

        assert sm.state == STATE_UNAVAILABLE
        assert sm.demand_on is demand_on

    @given(
        state_value=st.sampled_from(list(CONSISTENT_DEMAND)),
        demand_on=st.booleans(),
    )
    @settings(max_examples=100, deadline=None)
    def test_string_state_restores_consistently(
        self, state_value: ControllerState, demand_on: bool
    ):
        """Restoring from the state's string value yields the same invariant."""
        sm = _make_state_machine()

        sm.restore_state(state=state_value.value, demand_on=demand_on)

        assert sm.state == state_value
        assert sm.demand_on is CONSISTENT_DEMAND[state_value]

    @given(demand_on=st.booleans())
    @settings(max_examples=100, deadline=None)
    def test_invalid_string_resets_to_idle(self, demand_on: bool):
        """An unrecognised restored state resets to IDLE with demand off."""
        sm = _make_state_machine()

        sm.restore_state(state="not_a_real_state", demand_on=demand_on)

        assert sm.state == STATE_IDLE
        assert sm.demand_on is False
