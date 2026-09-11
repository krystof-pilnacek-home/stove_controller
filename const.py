"""Constants for the Stove Controller integration."""

DOMAIN = "stove_controller"

CONF_RELAY_ENTITY = "relay_entity"
CONF_MIN_ON_DURATION = "min_on_duration"
CONF_MIN_OFF_DURATION = "min_off_duration"

DEFAULT_MIN_ON_DURATION = 30
DEFAULT_MIN_OFF_DURATION = 25

STATE_IDLE = "idle"
STATE_HEATING = "heating"
STATE_PENDING_ON = "pending_on"
STATE_PENDING_OFF = "pending_off"
