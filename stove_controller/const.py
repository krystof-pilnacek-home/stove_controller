"""Constants for the Stove Controller integration."""

from enum import StrEnum
from typing import Final

DOMAIN: Final = "stove_controller"

CONF_RELAY_ENTITY: Final = "relay_entity"
CONF_MIN_ON_DURATION: Final = "min_on_duration"
CONF_MIN_OFF_DURATION: Final = "min_off_duration"
CONF_UPDATE_INTERVAL: Final = "update_interval"

DEFAULT_MIN_ON_DURATION: Final = 30
DEFAULT_MIN_OFF_DURATION: Final = 25
DEFAULT_UPDATE_INTERVAL: Final = 5


class ControllerState(StrEnum):
    """States for the stove controller state machine."""

    IDLE = "idle"
    HEATING = "heating"
    PENDING_ON = "pending_on"
    PENDING_OFF = "pending_off"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        return self.value


# Event constants for HA event bus communication
STOVE_DEMAND_CHANGED: Final = f"{DOMAIN}_demand_changed"


# Backwards compatibility aliases
STATE_IDLE: Final = ControllerState.IDLE
STATE_HEATING: Final = ControllerState.HEATING
STATE_PENDING_ON: Final = ControllerState.PENDING_ON
STATE_PENDING_OFF: Final = ControllerState.PENDING_OFF
STATE_UNAVAILABLE: Final = ControllerState.UNAVAILABLE
