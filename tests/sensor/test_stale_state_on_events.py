"""Tests for stale-state bugs triggered by incomplete event data.

Bug A — relay state-change event with old_state=None (HA restart):
    When HA restarts, async_track_state_change_event fires with
    old_state=None.  The handler must NOT treat this as a real state
    transition, otherwise _last_on / _last_off are overwritten with
    the current time, resetting the anti-short-cycle timers.

Bug B — demand-change event with demand_on=None (metadata-only event):
    An event carrying only entity_id metadata (no demand_on key) must
    not cancel an active wait or re-evaluate the state machine, otherwise
    the anti-short-cycle wait timer restarts from scratch unnecessarily.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from stove_controller.const import (
    STATE_HEATING,
    STATE_PENDING_OFF,
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


def _make_demand_event(
    entry_id: str = "test_entry_id",
    demand_on: bool | None = None,
    entity_id: str | None = None,
) -> MagicMock:
    """Build a mock demand-change event for _on_demand_change_event."""
    event = MagicMock()
    data: dict = {"entry_id": entry_id}
    if demand_on is not None:
        data["demand_on"] = demand_on
    if entity_id is not None:
        data["entity_id"] = entity_id
    event.data = data
    return event


# =========================================================================== #
# Bug A: _on_relay_change with old_state=None (HA restart)
# =========================================================================== #


class TestRelayChangeNoneOldState:
    """Relay state-change events with old_state=None must not corrupt timestamps.

    On HA restart the relay entity reports its initial state with
    old_state=None.  The handler must skip these events so that
    _last_on / _last_off retain their pre-restart values.
    """

    @pytest.mark.asyncio
    async def test_old_state_none_relay_on_preserves_timestamps(
        self, setup_sensor_hass
    ):
        """old_state=None, new_state=ON: _last_on must not be overwritten."""
        sensor, _ = setup_sensor_hass()
        original_last_on = _now() - timedelta(minutes=5)
        sensor._last_on = original_last_on
        sensor._last_off = None

        event = _make_relay_event(new_state_value=STATE_ON, old_state_value=None)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._last_on == original_last_on
        assert sensor._last_off is None

    @pytest.mark.asyncio
    async def test_old_state_none_relay_off_preserves_timestamps(
        self, setup_sensor_hass
    ):
        """old_state=None, new_state=OFF: _last_off must not be overwritten."""
        sensor, _ = setup_sensor_hass()
        original_last_off = _now() - timedelta(minutes=10)
        sensor._last_off = original_last_off
        sensor._last_on = None

        event = _make_relay_event(new_state_value=STATE_OFF, old_state_value=None)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._last_off == original_last_off
        assert sensor._last_on is None

    @pytest.mark.asyncio
    async def test_old_state_none_no_state_re_evaluation(self, setup_sensor_hass):
        """old_state=None: _apply_demand_logic must not be called."""
        sensor, _ = setup_sensor_hass()
        sensor._state = STATE_HEATING
        sensor._demand_on = True

        event = _make_relay_event(new_state_value=STATE_ON, old_state_value=None)

        with patch.object(
            sensor, "_apply_demand_logic", new=AsyncMock()
        ) as mock_logic:
            await sensor._on_relay_change(event)

        # _apply_demand_logic should not be called for old_state=None events
        mock_logic.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_state_change_still_updates_timestamps(self, setup_sensor_hass):
        """old_state=OFF, new_state=ON: _last_on is updated (real transition)."""
        sensor, _ = setup_sensor_hass()
        sensor._last_on = None
        sensor._last_off = _now() - timedelta(minutes=5)

        event = _make_relay_event(
            new_state_value=STATE_ON, old_state_value=STATE_OFF
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_relay_change(event)

        assert sensor._last_on == _now()


# =========================================================================== #
# Bug B: _on_demand_change_event with demand_on=None (metadata-only event)
# =========================================================================== #


class TestDemandEventNoneDemandOn:
    """Demand-change events without demand_on must not trigger state re-evaluation.

    An event carrying only entity_id metadata must update the entity_id
    but must not cancel an active wait or call _apply_demand_logic.
    """

    @pytest.mark.asyncio
    async def test_metadata_only_updates_entity_id(self, setup_sensor_hass):
        """Event with entity_id but no demand_on: entity_id updated, state unchanged."""
        sensor, hass = setup_sensor_hass()
        sensor._demand_on = False
        sensor._demand_entity_id = None
        sensor._state = STATE_HEATING
        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=35)

        event = _make_demand_event(
            entry_id="test_entry_id",
            entity_id="switch.demand_new",
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_demand_change_event(event)

        assert sensor._demand_entity_id == "switch.demand_new"
        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_metadata_only_preserves_active_wait(self, setup_sensor_hass):
        """Event without demand_on must not cancel or restart an active wait."""
        sensor, hass = setup_sensor_hass(min_on_min=30)
        sensor._demand_on = True
        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=15)

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor.handle_demand_change(demand_on=False)

        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is not None
        original_wait_task = sensor._wait_task
        original_wait_until = sensor._wait_until

        event = _make_demand_event(entry_id="test_entry_id")

        await sensor._on_demand_change_event(event)

        # Wait must NOT have been cancelled or restarted
        assert sensor._state == STATE_PENDING_OFF
        assert sensor._wait_task is original_wait_task
        assert sensor._wait_until == original_wait_until

        # Clean up the wait task
        sensor._cancel_wait()

    @pytest.mark.asyncio
    async def test_metadata_only_no_state_change(self, setup_sensor_hass):
        """Event without demand_on and without entity_id: no state change at all."""
        sensor, hass = setup_sensor_hass()
        sensor._demand_on = False
        sensor._state = STATE_HEATING
        hass.states.is_state.return_value = True
        sensor._last_on = _now() - timedelta(minutes=35)

        event = _make_demand_event(entry_id="test_entry_id")

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_demand_change_event(event)

        assert sensor._demand_on is False
        assert sensor._state == STATE_HEATING

    @pytest.mark.asyncio
    async def test_real_demand_change_still_works(self, setup_sensor_hass):
        """Event with demand_on=True: normal demand transition occurs."""
        sensor, hass = setup_sensor_hass(min_off_min=25)
        sensor._demand_on = False
        hass.states.is_state.return_value = False
        sensor._last_off = _now() - timedelta(minutes=30)

        event = _make_demand_event(
            entry_id="test_entry_id",
            demand_on=True,
            entity_id="switch.demand",
        )

        with patch("homeassistant.util.dt.now", return_value=_now()):
            await sensor._on_demand_change_event(event)

        assert sensor._demand_on is True
        assert sensor._demand_entity_id == "switch.demand"
        assert sensor._state == STATE_HEATING
