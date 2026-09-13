"""State machine for stove controller with anti-short-cycle protection.

This module provides an explicit state machine implementation for the stove
controller. It formalizes the state transitions that were previously
implicit in the sensor logic.

The state machine manages:
- IDLE: No demand, relay off
- HEATING: Demand on, relay on
- PENDING_ON: Demand on, waiting for min_off_duration before turning relay on
- PENDING_OFF: Demand off, waiting for min_on_duration before turning relay off
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .const import (
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
    ControllerState,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class StoveStateMachine:
    """Explicit state machine for stove controller anti-short-cycle logic.

    This class encapsulates all state transition logic, making it testable
    independently from the sensor entity. It provides:

    - Explicit state definitions
    - Valid transition rules
    - State transition methods
    - Wait timer management
    - Timestamp tracking for anti-short-cycle protection
    """

    # All valid state transitions
    VALID_TRANSITIONS = {
        STATE_IDLE: [STATE_HEATING, STATE_PENDING_ON, STATE_PENDING_OFF],
        STATE_HEATING: [STATE_IDLE, STATE_PENDING_OFF, STATE_PENDING_ON],
        STATE_PENDING_ON: [STATE_HEATING, STATE_IDLE],
        STATE_PENDING_OFF: [STATE_IDLE, STATE_HEATING],
    }

    def __init__(
        self,
        hass: HomeAssistant | None,
        relay_entity: str,
        min_on_duration: int,
        min_off_duration: int,
    ) -> None:
        """Initialize the state machine.

        Args:
            hass: Home Assistant instance (optional, for async operations)
            relay_entity: Entity ID of the relay switch
            min_on_duration: Minimum on duration in seconds
            min_off_duration: Minimum off duration in seconds
        """
        self.hass = hass
        self._relay_entity = relay_entity
        self._min_on_duration = min_on_duration
        self._min_off_duration = min_off_duration

        # State
        self._state: ControllerState = STATE_IDLE

        # Demand tracking
        self._demand_on: bool = False

        # Timestamp tracking
        self._last_on: datetime | None = None
        self._last_off: datetime | None = None
        self._wait_until: datetime | None = None

        # Wait task management with versioning to prevent race conditions
        self._wait_task: asyncio.Task | None = None
        self._wait_version: int = 0

        # State change callbacks
        self._on_state_change: (
            Callable[[ControllerState], Coroutine[Any, Any, None]] | None
        ) = None

        _LOGGER.debug(
            "StoveStateMachine initialized for %s with min_on=%s, min_off=%s",
            relay_entity, min_on_duration, min_off_duration
        )

    @property
    def state(self) -> ControllerState:
        """Current state of the machine."""
        return self._state

    @property
    def demand_on(self) -> bool:
        """Current demand state."""
        return self._demand_on

    @property
    def last_on(self) -> datetime | None:
        """Timestamp when relay was last turned on."""
        return self._last_on

    @property
    def last_off(self) -> datetime | None:
        """Timestamp when relay was last turned off."""
        return self._last_off

    @property
    def wait_until(self) -> datetime | None:
        """Timestamp when current wait will complete."""
        return self._wait_until

    @property
    def is_in_grace_period(self) -> bool:
        """True if in PENDING_ON or PENDING_OFF state."""
        return self._state in (STATE_PENDING_ON, STATE_PENDING_OFF)

    @property
    def wait_task(self) -> asyncio.Task | None:
        """Current wait task (for testing/cleanup)."""
        return self._wait_task

    @property
    def relay_entity(self) -> str:
        """Relay entity ID."""
        return self._relay_entity

    @property
    def min_on_duration(self) -> int:
        """Minimum on duration in seconds."""
        return self._min_on_duration

    @property
    def min_off_duration(self) -> int:
        """Minimum off duration in seconds."""
        return self._min_off_duration

    def register_on_state_change(
        self, callback: Callable[[ControllerState], Coroutine[Any, Any, None]]
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change = callback

    def get_remaining_seconds(self) -> int:
        """Get remaining wait time in seconds."""
        if self._wait_until:
            remaining = (self._wait_until - dt_util.now()).total_seconds()
            return max(0, int(remaining))
        return 0

    def compute_remaining(self, last_time: datetime | None, min_duration: int) -> int:
        """Compute remaining wait time in seconds.

        Public method for external use (e.g., tests).

        Args:
            last_time: Timestamp when the event occurred
            min_duration: Minimum duration in seconds

        Returns:
            Remaining time in seconds
        """
        return self._compute_remaining(last_time, min_duration)

    def is_valid_transition(self, target: ControllerState) -> bool:
        """Check if transition to target state is valid from current state."""
        valid_targets = self.VALID_TRANSITIONS.get(self._state, [])
        return target in valid_targets

    def get_relay_state(self) -> bool:
        """Get current relay state (ON=True, OFF=False).

        If hass is not available (e.g., during testing), returns False as a
        safe default.
        """
        if self.hass is None:
            return False  # Safe default when HA is not available
        return self.hass.states.is_state(self._relay_entity, "on")

    async def set_demand(self, demand_on: bool, relay_on: bool | None = None) -> None:
        """Set demand and trigger state transitions.

        Args:
            demand_on: New demand state
            relay_on: Optional current relay state (avoids HA call if provided)
        """
        self._demand_on = demand_on
        await self._apply_demand_logic(relay_on)

    async def sync_demand(self, demand_on: bool, relay_on: bool | None = None) -> None:
        """Sync demand state (for initialization).

        Args:
            demand_on: Demand state to sync
            relay_on: Optional current relay state
        """
        self._demand_on = demand_on
        await self._apply_demand_logic(relay_on)

    async def evaluate(self, relay_on: bool | None = None) -> None:
        """Evaluate current state based on demand and relay state.

        Public method to trigger state evaluation. Preferred over direct
        _evaluate_state calls.

        Args:
            relay_on: Optional current relay state (avoids HA call if provided)
        """
        await self._evaluate_state(relay_on)

    def cancel_wait(self) -> None:
        """Cancel any active wait timer.

        Public method to cancel waits. Preferred over direct _cancel_wait calls.
        """
        self._cancel_wait()

    async def update_relay_state(self, new_state: str) -> None:
        """Update internal tracking when relay state changes externally."""
        if new_state == "on":
            self._last_on = dt_util.now()
        elif new_state == "off":
            self._last_off = dt_util.now()
        else:
            _LOGGER.warning("Unknown relay state: %s", new_state)
            return

        # Pass the new relay state to avoid fetching it again
        relay_on = new_state == "on"
        await self._apply_demand_logic(relay_on)

    async def transition_to(self, target: ControllerState) -> bool:
        """Transition to a new state if valid."""
        if not self.is_valid_transition(target):
            _LOGGER.warning("Invalid transition from %s to %s", self._state, target)
            return False

        old_state = self._state
        self._state = target
        _LOGGER.debug("State transition: %s -> %s", old_state, target)

        if self._on_state_change:
            try:
                await self._on_state_change(target)
            except Exception as e:
                _LOGGER.error("Error in state change callback: %s", e)

        return True

    def restore_state(
        self,
        state: str | ControllerState,
        demand_on: bool = False,
        last_on: datetime | None = None,
        last_off: datetime | None = None,
        wait_until: datetime | None = None,
    ) -> None:
        """Restore state from saved data."""
        valid_states = {STATE_IDLE, STATE_HEATING, STATE_PENDING_ON, STATE_PENDING_OFF}

        # Try to convert string to ControllerState
        if isinstance(state, str):
            try:
                state = ControllerState(state)
            except ValueError:
                _LOGGER.warning("Invalid restored state: %s, resetting to IDLE", state)
                state = STATE_IDLE

        # Validate and potentially correct state
        if state not in valid_states:
            _LOGGER.warning("Invalid restored state: %s, resetting to IDLE", state)
            state = STATE_IDLE

        # Validate consistency and correct if needed
        if state == STATE_HEATING and not demand_on:
            demand_on = True
            _LOGGER.warning("Corrected demand_on to match HEATING state")
        elif state == STATE_IDLE and demand_on:
            demand_on = False
            _LOGGER.warning("Corrected demand_on to match IDLE state")
        elif state == STATE_PENDING_ON and not demand_on:
            demand_on = True
            _LOGGER.warning("Corrected demand_on to match PENDING_ON state")
        elif state == STATE_PENDING_OFF and demand_on:
            demand_on = False
            _LOGGER.warning("Corrected demand_on to match PENDING_OFF state")

        # Set all fields atomically
        self._state = state
        self._demand_on = demand_on
        self._last_on = last_on
        self._last_off = last_off
        self._wait_until = wait_until

        # Restore active wait if needed
        if wait_until and state in (STATE_PENDING_ON, STATE_PENDING_OFF):
            remaining = (wait_until - dt_util.now()).total_seconds()
            if remaining > 0:
                if state == STATE_PENDING_ON:
                    self._start_wait(int(remaining), self._complete_turn_on)
                elif state == STATE_PENDING_OFF:
                    self._start_wait(int(remaining), self._complete_turn_off)

    def _compute_remaining(
        self, last_time: datetime | None, min_duration: int
    ) -> int:
        """Compute remaining wait time in seconds."""
        if last_time is None:
            return min_duration
        elapsed = (dt_util.now() - last_time).total_seconds()
        return max(0, int(min_duration - elapsed))

    def _cancel_wait(self) -> None:
        """Cancel any existing wait task."""
        if self._wait_task and not self._wait_task.done():
            self._wait_task.cancel()
        self._wait_task = None
        self._wait_until = None

    def _start_wait(
        self,
        duration_sec: int,
        on_complete: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Start a wait task for the specified duration."""
        # Increment version to identify this wait task
        current_version = self._wait_version + 1
        self._cancel_wait()

        if duration_sec <= 0:
            self._wait_version = current_version
            if self.hass:
                self.hass.async_create_task(on_complete())
            else:
                asyncio.create_task(on_complete())
            return

        self._wait_until = dt_util.now() + timedelta(seconds=duration_sec)

        # Notify Home Assistant that a wait is now active so the time_remaining_sec
        # attribute (which derives from _wait_until) is published with the correct
        # value.  The transition to PENDING_* ran before _start_wait set
        # _wait_until, so without this the published state would still show 0.
        # This mirrors the pre-refactor behaviour where _start_wait called
        # async_write_ha_state() after setting _wait_until.  The notification
        # callback completes instantly, so tracking it is safe.
        if self._on_state_change is not None:
            coro = self._on_state_change(self._state)
            if self.hass:
                self.hass.async_create_task(coro)
            else:
                asyncio.create_task(coro)

        async def wait_and_execute(v=current_version) -> None:
            try:
                await asyncio.sleep(duration_sec)
                # Only execute if this is still the current wait
                if self._wait_version != v:
                    return
                self._wait_task = None
                self._wait_until = None
                await on_complete()
            except asyncio.CancelledError:
                # Only clear if this is still the current wait
                if self._wait_version == v:
                    self._wait_task = None
                    self._wait_until = None
                raise
            except Exception as e:
                _LOGGER.error("Error in wait task: %s", e)
                if self._wait_version == v:
                    self._wait_task = None
                    self._wait_until = None

        self._wait_version = current_version
        # Use a bare asyncio task (untracked by hass._tasks) for the wait timer
        # so hass.async_block_till_done() does not block until the (potentially
        # long) sleep elapses.  The pre-refactor code used asyncio.create_task
        # for the same reason; using hass.async_create_task here registers the
        # task in hass._tasks and makes the e2e tests hang indefinitely.
        self._wait_task = asyncio.create_task(wait_and_execute())

    async def _complete_turn_on(self) -> None:
        """Complete the turn on action after wait period."""
        # Guard against cancelled waits (state may have changed)
        if self._state != STATE_PENDING_ON:
            _LOGGER.debug(
                "_complete_turn_on called but not in PENDING_ON state "
                "(state=%s)", self._state
            )
            return

        if self._demand_on:
            await self._turn_on_relay()
            if self._state != STATE_HEATING:
                await self.transition_to(STATE_HEATING)
        else:
            if self._state != STATE_IDLE:
                await self.transition_to(STATE_IDLE)

    async def _complete_turn_off(self) -> None:
        """Complete the turn off action after wait period."""
        # Guard against cancelled waits (state may have changed)
        if self._state != STATE_PENDING_OFF:
            _LOGGER.debug(
                "_complete_turn_off called but not in PENDING_OFF state "
                "(state=%s)", self._state
            )
            return

        if not self._demand_on:
            await self._turn_off_relay()
            if self._state != STATE_IDLE:
                await self.transition_to(STATE_IDLE)
        else:
            if self._state != STATE_HEATING:
                await self.transition_to(STATE_HEATING)

    async def _turn_on_relay(self) -> None:
        """Send command to turn on relay."""
        if self.hass is None:
            # During testing, just update timestamp without HA call
            self._last_on = dt_util.now()
            return
        await self.hass.services.async_call(
            "switch", "turn_on",
            target={"entity_id": self._relay_entity},
            blocking=True,
        )
        self._last_on = dt_util.now()

    async def _turn_off_relay(self) -> None:
        """Send command to turn off relay."""
        if self.hass is None:
            # During testing, just update timestamp without HA call
            self._last_off = dt_util.now()
            return
        await self.hass.services.async_call(
            "switch", "turn_off",
            target={"entity_id": self._relay_entity},
            blocking=True,
        )
        self._last_off = dt_util.now()

    async def _apply_demand_logic(self, relay_on: bool | None = None) -> None:
        """Core state transition logic based on demand and relay state.

        Args:
            relay_on: Optional explicit relay state. If not provided, will be fetched.
        """
        # Cancel any existing wait before starting new logic
        self._cancel_wait()

        if self.hass is None and relay_on is None:
            # Cannot determine relay state without HA and no override
            return

        if relay_on is None:
            relay_on = self.get_relay_state()

        if self._demand_on and not relay_on:
            remaining = self._compute_remaining(self._last_off, self._min_off_duration)
            if remaining > 0:
                await self.transition_to(STATE_PENDING_ON)
                self._start_wait(remaining, self._complete_turn_on)
            else:
                await self._turn_on_relay()
                if self._state != STATE_HEATING:
                    await self.transition_to(STATE_HEATING)

        elif not self._demand_on and relay_on:
            remaining = self._compute_remaining(self._last_on, self._min_on_duration)
            if remaining > 0:
                await self.transition_to(STATE_PENDING_OFF)
                self._start_wait(remaining, self._complete_turn_off)
            else:
                await self._turn_off_relay()
                if self._state != STATE_IDLE:
                    await self.transition_to(STATE_IDLE)

        elif self._demand_on and relay_on:
            if self._state != STATE_HEATING:
                await self.transition_to(STATE_HEATING)

        else:
            if self._state != STATE_IDLE:
                await self.transition_to(STATE_IDLE)

    async def _evaluate_state(self, relay_on: bool | None = None) -> None:
        """Evaluate and set the correct state based on current conditions.

        Args:
            relay_on: Optional explicit relay state (avoids HA call if provided)
        """
        if self.hass is None:
            # During testing without HA, just use provided relay_on or default to False
            if relay_on is None:
                relay_on = False
            self._cancel_wait()
            await self._apply_demand_logic(relay_on)
            return

        if relay_on is None:
            relay_state = self.hass.states.get(self._relay_entity)
            if relay_state is None:
                _LOGGER.warning("Relay entity %s not available yet", self._relay_entity)
                if self._state != STATE_IDLE:
                    await self.transition_to(STATE_IDLE)
                return
            relay_on = relay_state.state == "on"

        self._cancel_wait()
        await self._apply_demand_logic(relay_on)
