"""Constants for the Stove Controller integration."""

from typing import Final

DOMAIN: Final = "stove_controller"

CONF_RELAY_ENTITY: Final = "relay_entity"
CONF_MIN_ON_DURATION: Final = "min_on_duration"
CONF_MIN_OFF_DURATION: Final = "min_off_duration"

DEFAULT_MIN_ON_DURATION: Final = 30
DEFAULT_MIN_OFF_DURATION: Final = 25

STATE_IDLE: Final = "idle"
STATE_HEATING: Final = "heating"
STATE_PENDING_ON: Final = "pending_on"
STATE_PENDING_OFF: Final = "pending_off"
