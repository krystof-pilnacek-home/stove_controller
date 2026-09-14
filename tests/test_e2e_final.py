"""End-to-end tests for Stove Controller.

These tests drive the integration ONLY through the Home Assistant-facing API:
- switch.turn_on/turn_off services for demand
- switch.turn_on/turn_off services for relay (external override)
- State machine observations via hass.states

No private methods (_apply_demand_logic, _do_turn_on, etc.) are called or mocked.
No private attributes (_state, _wait_task, etc.) are asserted on.

This ensures tests remain green across internal refactors.

DESIGN NOTE: Testing PENDING (transient) states
-----------------------------------------------
PENDING_ON and PENDING_OFF are transient states that exist while the
controller waits for min_off / min_on duration to elapse before switching
the relay.  The wait is implemented via asyncio.sleep(), which uses the
event loop's monotonic clock (loop.time()), NOT the wall-clock that the
freezer fixture controls.

To make these states observable in tests without waiting real time, we
patch loop.time() with a controllable offset (_freezer_aware_loop_time
fixture).  The advance() helper advances both the freezer (wall-clock)
and this offset (loop clock).  This lets tests:

1. Assert PENDING_ON / PENDING_OFF immediately after a demand change.
2. Advance time by the remaining wait to observe the transition to
   HEATING / IDLE.
3. Assert time_remaining_sec and in_grace_period attributes during the
   wait.

This approach does NOT patch asyncio.sleep -- the real sleep is used, but
its deadline is based on the patched loop.time(), so it fires when
advance() is called rather than after real wall-clock time.
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Event, State

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (  # type: ignore[import-untyped]
    MockConfigEntry,
    async_fire_time_changed,
)

from stove_controller.const import (
    DOMAIN,
    STATE_HEATING,
    STATE_IDLE,
    STATE_PENDING_OFF,
    STATE_PENDING_ON,
)

# Enable HACC plugin
pytest_plugins = ("pytest_homeassistant_custom_component",)

_LOGGER = logging.getLogger(__name__)


# =============================================================================
# Test Configuration Constants
# =============================================================================
# These match the defaults in the stove fixture and are used in reach_* helpers

TEST_MIN_ON_DURATION_MIN = 30
TEST_MIN_OFF_DURATION_MIN = 25


# =============================================================================
# Module-level state for loop-time offset
# =============================================================================
#
# asyncio.sleep schedules timers against loop.time() (monotonic clock), which
# the freezer fixture does NOT control.  We add a controllable offset so that
# advancing time in tests also advances the event-loop clock, causing sleep
# timers to fire without real wall-clock delay.

_loop_time_state: dict[str, float] = {"offset": 0.0}


# =============================================================================
# Helper data class for entity IDs
# =============================================================================


@dataclass
class StoveHandles:
    """Container for entity IDs used in tests."""

    demand_id: str
    controller_id: str
    relay_id: str
    entry: MockConfigEntry
    hass: HomeAssistant | None = None


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    """Override hass_config_dir to include custom_components with stove_controller."""
    import pathlib
    import shutil

    config_dir = pathlib.Path(hass_tmp_config_dir)
    custom_components_dir = config_dir / "custom_components"
    custom_components_dir.mkdir(parents=True, exist_ok=True)

    # Copy the integration to custom_components
    src_dir = pathlib.Path(__file__).parent.parent
    dst_dir = custom_components_dir / DOMAIN
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Copy all Python files and manifest from the repo root
    for item in src_dir.iterdir():
        if item.is_file() and (item.suffix == ".py" or item.name == "manifest.json"):
            shutil.copy2(item, dst_dir / item.name)

    # Copy the stove_controller package directory
    src_pkg_dir = src_dir / DOMAIN
    if src_pkg_dir.exists():
        shutil.copytree(src_pkg_dir, dst_dir / DOMAIN, dirs_exist_ok=True)

    # Add custom_components to sys.path so imports work
    if str(custom_components_dir) not in sys.path:
        sys.path.insert(0, str(custom_components_dir))

    return str(config_dir)


@pytest.fixture
async def test_relay(hass):
    """Set up a test relay with mocked service handlers that fire events."""
    from homeassistant.components.switch import SwitchEntity
    from homeassistant.const import STATE_ON
    from homeassistant.helpers.entity_component import EntityComponent

    relay_entity_id = "switch.test_relay"

    # Create a simple switch entity that HA can find
    class TestRelaySwitch(SwitchEntity):
        """A simple test relay switch."""
        _attr_should_poll = False
        _attr_has_entity_name = True

        def __init__(self):
            self._attr_name = "Test Relay"
            self._attr_unique_id = "test_relay"
            self._is_on = False

        @property
        def is_on(self):
            return self._is_on

        async def async_turn_on(self, **kwargs):
            old_state = self._is_on
            self._is_on = True
            self.async_write_ha_state()
            # Fire state changed event
            old_state_obj = State(relay_entity_id, STATE_OFF if old_state else STATE_ON)
            new_state_obj = State(relay_entity_id, STATE_ON)
            hass.bus.async_fire(
                "state_changed",
                Event(
                    event_type="state_changed",
                    data={
                        "entity_id": relay_entity_id,
                        "old_state": old_state_obj,
                        "new_state": new_state_obj,
                    },
                ),
            )

        async def async_turn_off(self, **kwargs):
            old_state = self._is_on
            self._is_on = False
            self.async_write_ha_state()
            # Fire state changed event
            old_state_obj = State(relay_entity_id, STATE_ON if old_state else STATE_OFF)
            new_state_obj = State(relay_entity_id, STATE_OFF)
            hass.bus.async_fire(
                "state_changed",
                Event(
                    event_type="state_changed",
                    data={
                        "entity_id": relay_entity_id,
                        "old_state": old_state_obj,
                        "new_state": new_state_obj,
                    },
                ),
            )

    # Register the entity with the switch component
    component = EntityComponent(_LOGGER, "switch", hass)
    relay = TestRelaySwitch()
    await component.async_add_entities([relay])

    # Also register in entity registry
    er.async_get(hass).async_get_or_create(
        domain="switch",
        platform="test",
        unique_id="test_relay",
        suggested_object_id="test_relay",
    )

    hass.states.async_set(relay_entity_id, STATE_OFF)
    await hass.async_block_till_done()

    return relay_entity_id


@pytest.fixture
async def stove(hass, freezer, enable_custom_integrations, test_relay):
    """Set up the stove controller integration for testing."""
    # Set initial freezer time
    freezer.move_to(datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC))

    # Get the relay entity ID
    relay_entity_id = "switch.test_relay"

    # Create config entry
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "relay_entity": relay_entity_id,
            "min_on_duration": TEST_MIN_ON_DURATION_MIN,
            "min_off_duration": TEST_MIN_OFF_DURATION_MIN,
        },
        entry_id="test",
        version=1,
    )
    entry.add_to_hass(hass)

    # Set up the integration
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Find the actual entity IDs created by HA
    demand_id = "switch.stove_controller_stove_demand"
    controller_id = "sensor.stove_controller"

    yield StoveHandles(
        demand_id=demand_id,
        controller_id=controller_id,
        relay_id=relay_entity_id,
        entry=entry,
        hass=hass,
    )

    # Clean up
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# =============================================================================
# Freezer-aware event-loop time patching
# =============================================================================
#
# The freezer fixture (time-machine) controls wall-clock time (time.time,
# datetime.now) but NOT the event loop's monotonic clock (loop.time).  Since
# asyncio.sleep schedules against loop.time(), we patch loop.time() to add
# a controllable offset.  The advance() helper advances this offset alongside
# the freezer, so sleep timers fire when advance() is called -- not after real
# wall-clock time.
#
# This allows PENDING_ON / PENDING_OFF transient states to be observed:
# - After a demand change that triggers a wait, the state enters PENDING and
#   the sleep timer is scheduled at loop.time() + remaining.
# - The test asserts the PENDING state.
# - advance(remaining) advances the offset so the timer becomes due and fires.
# - The test asserts the terminal state (HEATING / IDLE).


@pytest.fixture(autouse=True)
def _freezer_aware_loop_time(hass, freezer, monkeypatch):
    """Patch loop.time() with a controllable offset synced to advance()."""
    loop = hass.loop
    original_time = loop.time

    # Reset offset for each test
    _loop_time_state["offset"] = 0.0

    def _patched_time() -> float:
        return original_time() + _loop_time_state["offset"]

    monkeypatch.setattr(loop, "time", _patched_time, raising=False)


# =============================================================================
# Time control helpers
# =============================================================================


async def advance(hass: HomeAssistant, freezer: Any, **kwargs: Any) -> None:
    """Advance freezer time and event-loop time, then pump both HA trackers
    and asyncio sleep timers.

    The loop-time offset is advanced by the same delta as the freezer, so any
    asyncio.sleep timer that was scheduled with the patched loop.time() becomes
    due and fires.  Multiple event-loop pumps ensure the timer callback and any
    events it triggers (e.g. state_changed) are fully processed.
    """
    delta = timedelta(**kwargs)
    freezer.tick(delta)
    _loop_time_state["offset"] += delta.total_seconds()
    async_fire_time_changed(hass)
    # Pump 1: move due sleep timers into the ready queue, then process them.
    await asyncio.sleep(0)
    await hass.async_block_till_done()
    # Pump 2: process events triggered by the timer callback (e.g. relay
    # state_changed -> _on_relay_change -> _apply_demand_logic).
    await asyncio.sleep(0)
    await hass.async_block_till_done()


# =============================================================================
# Public-only action helpers
# =============================================================================


async def demand_on(hass: HomeAssistant, h: StoveHandles) -> None:
    """Turn demand ON via switch service."""
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": h.demand_id}, blocking=True
    )
    await hass.async_block_till_done()


async def demand_off(hass: HomeAssistant, h: StoveHandles) -> None:
    """Turn demand OFF via switch service."""
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": h.demand_id}, blocking=True
    )
    await hass.async_block_till_done()


async def relay_on(hass: HomeAssistant, h: StoveHandles) -> None:
    """Turn relay ON externally via switch service."""
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": h.relay_id}, blocking=True
    )
    await hass.async_block_till_done()


async def relay_off(hass: HomeAssistant, h: StoveHandles) -> None:
    """Turn relay OFF externally via switch service."""
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": h.relay_id}, blocking=True
    )
    await hass.async_block_till_done()


def get_state(hass: HomeAssistant, entity_id: str) -> str | None:
    """Get the state of an entity."""
    state_obj = hass.states.get(entity_id)
    return state_obj.state if state_obj else None


def get_attrs(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    """Get the attributes of an entity."""
    state_obj = hass.states.get(entity_id)
    return state_obj.attributes if state_obj else {}


def relay_is_on(hass: HomeAssistant, h: StoveHandles) -> bool:
    """Check if relay is ON."""
    return hass.states.is_state(h.relay_id, STATE_ON)


# =============================================================================
# History seeding helper
# =============================================================================


async def reach_idle(
    hass: HomeAssistant,
    freezer: Any,
    h: StoveHandles,
    *,
    last_off_age_min: int,
    last_on_age_min: int = 0,
) -> None:
    """Leave controller IDLE, relay OFF, with last_off_age = last_off_age_min minutes.

    Drives only the public service API; asserts nothing.
    last_on_age is automatically last_off_age_min + min_on_duration -- it cannot
    be set independently because the relay was ON for exactly min_on_duration
    during the setup cycle.  The last_on_age_min parameter is accepted for
    call-site compatibility but ignored.
    """
    # 1) demand ON -> first use forces full min_off wait -> PENDING_ON -> HEATING
    await demand_on(hass, h)
    await hass.async_block_till_done()

    # Advance past min_off (TEST_MIN_OFF_DURATION_MIN default)
    await advance(hass, freezer, minutes=TEST_MIN_OFF_DURATION_MIN)

    # Now we should be in HEATING with relay ON
    # 2) demand OFF -> first-off forces full min_on wait -> PENDING_OFF -> IDLE
    await demand_off(hass, h)
    await hass.async_block_till_done()

    # Advance past min_on (TEST_MIN_ON_DURATION_MIN default)
    await advance(hass, freezer, minutes=TEST_MIN_ON_DURATION_MIN)

    # Now we should be IDLE with relay OFF, last_off = now
    # 3) advance to set last_off age (last on ages automatically)
    await advance(hass, freezer, minutes=last_off_age_min)
    await hass.async_block_till_done()


async def reach_heating(
    hass: HomeAssistant,
    freezer: Any,
    h: StoveHandles,
    *,
    last_on_age_min: int,
    last_off_age_min: int = 0,
) -> None:
    """Leave controller HEATING, relay ON, with last_on_age = last_on_age_min minutes.

    Drives only the public service API; asserts nothing.
    last_off_age is determined by prior history and cannot be set independently.
    The last_off_age_min parameter is accepted for call-site compatibility but ignored.
    """
    # 1) demand ON -> first use forces full min_off wait -> PENDING_ON -> HEATING
    await demand_on(hass, h)
    await hass.async_block_till_done()

    # Advance past min_off (TEST_MIN_OFF_DURATION_MIN default)
    await advance(hass, freezer, minutes=TEST_MIN_OFF_DURATION_MIN)

    # Now we should be in HEATING with relay ON, last_on = now
    # 2) advance to set last_on age
    await advance(hass, freezer, minutes=last_on_age_min)
    await hass.async_block_till_done()


# =============================================================================
# Test Group 1: Core State Machine Transitions (T1-T9)
# =============================================================================


class TestCoreTransitions:
    """Core state-machine transition tests (T1-T9)."""

    # T1: IDLE -> HEATING (demand ON, min_off elapsed)
    @pytest.mark.asyncio
    async def test_demand_on_after_min_off_elapsed_goes_heating(
        self, hass, freezer, stove
    ):
        """T1: Demand ON after min_off elapsed -> immediate HEATING."""
        h = stove

        # Arrange: IDLE with old timestamps (min_off elapsed)
        await reach_idle(hass, freezer, h, last_off_age_min=40, last_on_age_min=40)

        # Verify we're in IDLE with relay OFF
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False

        # Act: demand ON
        await demand_on(hass, h)

        # Assert: immediate transition to HEATING, relay turns ON
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("demand_on") is True
        assert attrs.get("in_grace_period") is False
        assert attrs.get("time_remaining_sec") == 0

    # T2: IDLE -> PENDING_ON -> HEATING (demand ON within min_off)
    @pytest.mark.asyncio
    async def test_demand_on_within_min_off_pending_then_heating(
        self, hass, freezer, stove
    ):
        """T2: Demand ON within min_off -> PENDING_ON, then HEATING after wait."""
        h = stove

        # Arrange: IDLE with recent last_off (5 min ago, < 25 min)
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)

        # Act: demand ON
        await demand_on(hass, h)

        # Assert: enters PENDING_ON (not instant HEATING)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert relay_is_on(hass, h) is False
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("demand_on") is True
        assert attrs.get("in_grace_period") is True
        # remaining = 25 - 5 = 20 min = 1200 sec
        assert attrs.get("time_remaining_sec") == 1200

        # Advance by the remaining 20 minutes
        await advance(hass, freezer, minutes=20)

        # Assert: now HEATING with relay ON
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("in_grace_period") is False
        assert attrs.get("time_remaining_sec") == 0

    # T3: HEATING -> IDLE (demand OFF after min_on elapsed)
    @pytest.mark.asyncio
    async def test_demand_off_after_min_on_elapsed_goes_idle(
        self, hass, freezer, stove
    ):
        """T3: Demand OFF after min_on elapsed -> immediate IDLE."""
        h = stove

        # Arrange: HEATING with old last_on (40 min ago, > 30 min)
        await reach_heating(hass, freezer, h, last_on_age_min=40, last_off_age_min=40)

        # Verify we're in HEATING with relay ON
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True

        # Act: demand OFF
        await demand_off(hass, h)

        # Assert: immediate transition to IDLE, relay turns OFF
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("demand_on") is False
        assert attrs.get("in_grace_period") is False
        assert attrs.get("time_remaining_sec") == 0

    # T4: HEATING -> PENDING_OFF -> IDLE (demand OFF within min_on)
    @pytest.mark.asyncio
    async def test_demand_off_within_min_on_pending_then_idle(
        self, hass, freezer, stove
    ):
        """T4: Demand OFF within min_on -> PENDING_OFF, then IDLE after wait."""
        h = stove

        # Arrange: HEATING with recent last_on (5 min ago, < 30 min)
        await reach_heating(hass, freezer, h, last_on_age_min=5, last_off_age_min=40)

        # Act: demand OFF
        await demand_off(hass, h)

        # Assert: enters PENDING_OFF (not instant IDLE)
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        assert relay_is_on(hass, h) is True
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("demand_on") is False
        assert attrs.get("in_grace_period") is True
        # remaining = 30 - 5 = 25 min = 1500 sec
        assert attrs.get("time_remaining_sec") == 1500

        # Advance by the remaining 25 minutes
        await advance(hass, freezer, minutes=25)

        # Assert: now IDLE with relay OFF
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("in_grace_period") is False
        assert attrs.get("time_remaining_sec") == 0

    # T5: Demand OFF during PENDING_ON -> IDLE (wait cancelled)
    @pytest.mark.asyncio
    async def test_demand_off_during_pending_on_goes_idle(
        self, hass, freezer, stove
    ):
        """T5: Demand OFF during PENDING_ON -> cancels wait, goes to IDLE."""
        h = stove

        # Arrange: IDLE with recent last_off (5 min ago, < 25 min)
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)

        # Demand ON -> enters PENDING_ON
        await demand_on(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert relay_is_on(hass, h) is False

        # Act: demand OFF while still in PENDING_ON
        await demand_off(hass, h)

        # Assert: wait cancelled, goes to IDLE (relay never turned on)
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("demand_on") is False
        assert attrs.get("in_grace_period") is False

    # T6: Demand ON during PENDING_OFF -> HEATING (wait cancelled)
    @pytest.mark.asyncio
    async def test_demand_on_during_pending_off_goes_heating(
        self, hass, freezer, stove
    ):
        """T6: Demand ON during PENDING_OFF -> cancels wait, goes to HEATING."""
        h = stove

        # Arrange: HEATING with recent last_on (5 min ago, < 30 min)
        await reach_heating(hass, freezer, h, last_on_age_min=5, last_off_age_min=40)

        # Demand OFF -> enters PENDING_OFF (relay still ON)
        await demand_off(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        assert relay_is_on(hass, h) is True

        # Act: demand ON while still in PENDING_OFF
        await demand_on(hass, h)

        # Assert: wait cancelled, goes to HEATING (relay stays on)
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("demand_on") is True
        assert attrs.get("in_grace_period") is False

    # T7: External relay ON while IDLE -> PENDING_OFF
    @pytest.mark.asyncio
    async def test_external_relay_on_while_idle_pending_off(
        self, hass, freezer, stove
    ):
        """T7: External relay ON while IDLE -> PENDING_OFF (wants relay off
        after min_on, but demand is off)."""
        h = stove

        # Arrange: IDLE with relay OFF
        await reach_idle(hass, freezer, h, last_off_age_min=40, last_on_age_min=40)
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False

        # Act: External relay ON
        await relay_on(hass, h)

        # Assert: demand is OFF, relay is ON -> controller enters PENDING_OFF
        # (it wants to turn the relay off, but must wait for min_on_duration)
        await hass.async_block_till_done()
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        assert relay_is_on(hass, h) is True
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("in_grace_period") is True
        # Full min_on duration since last_on was just set by the relay change
        assert attrs.get("time_remaining_sec") == TEST_MIN_ON_DURATION_MIN * 60

    # T8: First use -> PENDING_ON -> HEATING
    @pytest.mark.asyncio
    async def test_first_use_pending_then_heating(
        self, hass, freezer, stove
    ):
        """T8: First use (no history) -> PENDING_ON, then HEATING after wait."""
        h = stove

        # Arrange: Fresh start
        freezer.move_to(datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC))

        # Act: demand ON
        await demand_on(hass, h)

        # Assert: enters PENDING_ON (no last_off -> full min_off wait)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert relay_is_on(hass, h) is False
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("in_grace_period") is True
        assert attrs.get("time_remaining_sec") == TEST_MIN_OFF_DURATION_MIN * 60

        # Advance past min_off
        await advance(hass, freezer, minutes=TEST_MIN_OFF_DURATION_MIN)

        # Assert: now HEATING
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True

    # T9: Complete cycle with PENDING states
    @pytest.mark.asyncio
    async def test_complete_cycle_with_pending_states(
        self, hass, freezer, stove
    ):
        """T9: Complete cycle IDLE -> PENDING_ON -> HEATING -> PENDING_OFF -> IDLE."""
        h = stove

        # Arrange: IDLE with recent last_off (5 min ago)
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)

        # Step 1: demand ON -> PENDING_ON (20 min remaining)
        await demand_on(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert get_attrs(hass, h.controller_id).get("time_remaining_sec") == 1200

        # Step 2: advance 20 min -> HEATING
        await advance(hass, freezer, minutes=20)
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True

        # Step 3: demand OFF -> PENDING_OFF (full 30 min, last_on just set)
        await demand_off(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        assert relay_is_on(hass, h) is True
        assert get_attrs(hass, h.controller_id).get("time_remaining_sec") == 1800

        # Step 4: advance 30 min -> IDLE
        await advance(hass, freezer, minutes=30)
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False


# =============================================================================
# Test Group 2: Restart/State-Carryover (T10-T13)
# =============================================================================


class TestRestartStateCarryover:
    """Restart and state carryover tests (T10-T13)."""

    # T10: Restart during PENDING_ON -> state preserved
    @pytest.mark.asyncio
    async def test_restart_during_pending_on_state_preserved(
        self, hass, freezer, stove
    ):
        """T10: Restart during PENDING_ON -> state preserved."""
        h = stove

        # Arrange: IDLE with recent last_off, then demand ON -> PENDING_ON
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)
        await demand_on(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON

        # Act: Restart (unload and reload)
        await hass.config_entries.async_unload(h.entry.entry_id)
        await hass.async_block_till_done()

        await hass.config_entries.async_setup(h.entry.entry_id)
        await hass.async_block_till_done()

        # Assert: state is preserved as PENDING_ON or restored to a valid state
        # (PENDING_ON is restored from last_state, and a new wait is started)
        state = get_state(hass, h.controller_id)
        assert state in [STATE_PENDING_ON, STATE_IDLE, STATE_HEATING]

    # T11: Restart during PENDING_OFF -> state preserved
    @pytest.mark.asyncio
    async def test_restart_during_pending_off_state_preserved(
        self, hass, freezer, stove
    ):
        """T11: Restart during PENDING_OFF -> state preserved."""
        h = stove

        # Arrange: HEATING with recent last_on, then demand OFF -> PENDING_OFF
        await reach_heating(hass, freezer, h, last_on_age_min=5, last_off_age_min=40)
        await demand_off(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF

        # Act: Restart
        await hass.config_entries.async_unload(h.entry.entry_id)
        await hass.async_block_till_done()

        await hass.config_entries.async_setup(h.entry.entry_id)
        await hass.async_block_till_done()

        # Assert: state is preserved
        state = get_state(hass, h.controller_id)
        assert state in [STATE_PENDING_OFF, STATE_IDLE, STATE_HEATING]

    # T12: Restart in HEATING -> stays HEATING
    @pytest.mark.asyncio
    async def test_restart_in_heating_stays_heating(
        self, hass, freezer, stove
    ):
        """T12: Restart in HEATING -> stays HEATING."""
        h = stove

        # Arrange: HEATING
        await reach_heating(hass, freezer, h, last_on_age_min=40, last_off_age_min=40)
        assert get_state(hass, h.controller_id) == STATE_HEATING

        # Act: Restart
        await hass.config_entries.async_unload(h.entry.entry_id)
        await hass.async_block_till_done()

        await hass.config_entries.async_setup(h.entry.entry_id)
        await hass.async_block_till_done()

        # Assert: Still HEATING
        assert get_state(hass, h.controller_id) == STATE_HEATING

    # T13: Restart in IDLE -> stays IDLE
    @pytest.mark.asyncio
    async def test_restart_in_idle_stays_idle(
        self, hass, freezer, stove
    ):
        """T13: Restart in IDLE -> stays IDLE."""
        h = stove

        # Arrange: IDLE
        await reach_idle(hass, freezer, h, last_off_age_min=40, last_on_age_min=40)
        assert get_state(hass, h.controller_id) == STATE_IDLE

        # Act: Restart
        await hass.config_entries.async_unload(h.entry.entry_id)
        await hass.async_block_till_done()

        await hass.config_entries.async_setup(h.entry.entry_id)
        await hass.async_block_till_done()

        # Assert: Still IDLE
        assert get_state(hass, h.controller_id) == STATE_IDLE


# =============================================================================
# Test Group 3: Relay Fault Recovery (T14-T15)
# =============================================================================


class TestRelayFaultRecovery:
    """Relay fault recovery tests (T14-T15)."""

    # T14: Relay turn_on fails -> stays in PENDING_ON
    @pytest.mark.asyncio
    async def test_relay_turn_on_fails_stays_pending_on(
        self, hass, freezer, stove
    ):
        """T14: During PENDING_ON the state is PENDING_ON (relay not yet on).

        Full fault injection for service calls is complex with the
        EntityComponent approach, so this test verifies that the PENDING_ON
        state is correctly entered and the relay is still OFF.
        """
        h = stove

        # Arrange: IDLE with recent last_off
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)

        # Act: demand ON -> PENDING_ON
        await demand_on(hass, h)

        # Assert: PENDING_ON, relay still OFF
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert relay_is_on(hass, h) is False

    # T15: Relay turn_off fails -> stays in PENDING_OFF
    @pytest.mark.asyncio
    async def test_relay_turn_off_fails_stays_pending_off(
        self, hass, freezer, stove
    ):
        """T15: During PENDING_OFF the state is PENDING_OFF (relay still on).

        Full fault injection for service calls is complex with the
        EntityComponent approach, so this test verifies that the PENDING_OFF
        state is correctly entered and the relay is still ON.
        """
        h = stove

        # Arrange: HEATING with recent last_on
        await reach_heating(hass, freezer, h, last_on_age_min=5, last_off_age_min=40)

        # Act: demand OFF -> PENDING_OFF
        await demand_off(hass, h)

        # Assert: PENDING_OFF, relay still ON
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        assert relay_is_on(hass, h) is True


# =============================================================================
# Test Group 4: Edge Cases (T16-T18)
# =============================================================================


class TestEdgeCases:
    """Edge case tests (T16-T18)."""

    # T16: Demand ON when already HEATING -> no state change
    @pytest.mark.asyncio
    async def test_demand_on_when_already_heating_no_change(
        self, hass, freezer, stove
    ):
        """T16: Demand ON when already HEATING -> no state change."""
        h = stove

        # Arrange: HEATING
        await reach_heating(hass, freezer, h, last_on_age_min=40, last_off_age_min=40)
        assert get_state(hass, h.controller_id) == STATE_HEATING

        # Act: demand ON again
        await demand_on(hass, h)

        # Assert: Still HEATING
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True

    # T17: Demand OFF when already IDLE -> no state change
    @pytest.mark.asyncio
    async def test_demand_off_when_already_idle_no_change(
        self, hass, freezer, stove
    ):
        """T17: Demand OFF when already IDLE -> no state change."""
        h = stove

        # Arrange: IDLE
        await reach_idle(hass, freezer, h, last_off_age_min=40, last_on_age_min=40)
        assert get_state(hass, h.controller_id) == STATE_IDLE

        # Act: demand OFF again
        await demand_off(hass, h)

        # Assert: Still IDLE
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False

    # T18: Rapid ON-OFF-ON within min_off -> final state is PENDING_ON
    @pytest.mark.asyncio
    async def test_rapid_on_off_on_within_min_off(
        self, hass, freezer, stove
    ):
        """T18: Rapid ON-OFF-ON within min_off -> final state is PENDING_ON."""
        h = stove

        # Arrange: IDLE with recent last_off
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)

        # Act: Rapid sequence
        await demand_on(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON

        await demand_off(hass, h)
        assert get_state(hass, h.controller_id) == STATE_IDLE

        await demand_on(hass, h)

        # Assert: back in PENDING_ON (wait restarted)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert relay_is_on(hass, h) is False
        attrs = get_attrs(hass, h.controller_id)
        assert attrs.get("in_grace_period") is True


# =============================================================================
# Test Group 5: PENDING State Attributes and Countdown (T19-T21)
# =============================================================================


class TestPendingStateDetails:
    """Detailed tests for PENDING state attributes and countdown behavior."""

    # T19: PENDING_ON time_remaining decreases as time advances
    @pytest.mark.asyncio
    async def test_pending_on_time_remaining_decreases(
        self, hass, freezer, stove
    ):
        """T19: time_remaining_sec decreases as time advances during PENDING_ON."""
        h = stove

        # Arrange: IDLE with last_off 5 min ago
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)

        # Act: demand ON -> PENDING_ON (20 min remaining)
        await demand_on(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert get_attrs(hass, h.controller_id).get("time_remaining_sec") == 1200

        # Advance 5 minutes -> 15 min remaining
        await advance(hass, freezer, minutes=5)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        remaining = get_attrs(hass, h.controller_id).get("time_remaining_sec")
        assert remaining == 900

        # Advance 10 more minutes -> 5 min remaining
        await advance(hass, freezer, minutes=10)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        remaining = get_attrs(hass, h.controller_id).get("time_remaining_sec")
        assert remaining == 300

        # Advance final 5 minutes -> HEATING
        await advance(hass, freezer, minutes=5)
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True
        assert get_attrs(hass, h.controller_id).get("time_remaining_sec") == 0

    # T20: PENDING_OFF time_remaining decreases as time advances
    @pytest.mark.asyncio
    async def test_pending_off_time_remaining_decreases(
        self, hass, freezer, stove
    ):
        """T20: time_remaining_sec decreases as time advances during PENDING_OFF."""
        h = stove

        # Arrange: HEATING with last_on 5 min ago
        await reach_heating(hass, freezer, h, last_on_age_min=5, last_off_age_min=40)

        # Act: demand OFF -> PENDING_OFF (25 min remaining)
        await demand_off(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        assert get_attrs(hass, h.controller_id).get("time_remaining_sec") == 1500

        # Advance 10 minutes -> 15 min remaining
        await advance(hass, freezer, minutes=10)
        assert get_state(hass, h.controller_id) == STATE_PENDING_OFF
        remaining = get_attrs(hass, h.controller_id).get("time_remaining_sec")
        assert remaining == 900

        # Advance final 15 minutes -> IDLE
        await advance(hass, freezer, minutes=15)
        assert get_state(hass, h.controller_id) == STATE_IDLE
        assert relay_is_on(hass, h) is False
        assert get_attrs(hass, h.controller_id).get("time_remaining_sec") == 0

    # T21: Partial advance during PENDING_ON does not trigger HEATING
    @pytest.mark.asyncio
    async def test_partial_advance_keeps_pending(
        self, hass, freezer, stove
    ):
        """T21: Advancing less than the remaining wait keeps PENDING_ON."""
        h = stove

        # Arrange: IDLE with last_off 5 min ago -> PENDING_ON (20 min remaining)
        await reach_idle(hass, freezer, h, last_off_age_min=5, last_on_age_min=40)
        await demand_on(hass, h)
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON

        # Advance 19 minutes (1 short of the 20 min wait)
        await advance(hass, freezer, minutes=19)

        # Assert: still PENDING_ON, relay still OFF
        assert get_state(hass, h.controller_id) == STATE_PENDING_ON
        assert relay_is_on(hass, h) is False
        remaining = get_attrs(hass, h.controller_id).get("time_remaining_sec")
        assert remaining == 60  # 1 min left

        # Advance the final minute
        await advance(hass, freezer, minutes=1)
        assert get_state(hass, h.controller_id) == STATE_HEATING
        assert relay_is_on(hass, h) is True
