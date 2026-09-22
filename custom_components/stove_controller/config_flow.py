"""Config flow for the Stove Controller integration."""

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_MIN_OFF_DURATION,
    CONF_MIN_ON_DURATION,
    CONF_RELAY_ENTITY,
    CONF_UPDATE_INTERVAL,
    DEFAULT_MIN_OFF_DURATION,
    DEFAULT_MIN_ON_DURATION,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_RELAY_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="switch")
        ),
        vol.Required(
            CONF_MIN_ON_DURATION, default=DEFAULT_MIN_ON_DURATION
        ): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=180,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_MIN_OFF_DURATION, default=DEFAULT_MIN_OFF_DURATION
        ): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=180,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_RELAY_ENTITY): EntitySelector(
            EntitySelectorConfig(domain="switch")
        ),
        vol.Required(CONF_MIN_ON_DURATION): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=180,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_MIN_OFF_DURATION): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=180,
                step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL
        ): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=60,
                step=1,
                unit_of_measurement="s",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)


class StoveControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Stove Controller."""

    VERSION: int = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Stove Controller", data=user_input)
        return self.async_show_form(step_id="user", data_schema=CONFIG_SCHEMA)

    @classmethod
    @callback
    def async_get_options_flow(cls, config_entry):
        """Get the options flow."""
        return StoveControllerOptionsFlow()


class StoveControllerOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle options flow with automatic reload on change."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        suggested_values = {
            CONF_RELAY_ENTITY: current.get(CONF_RELAY_ENTITY),
            CONF_MIN_ON_DURATION: current.get(
                CONF_MIN_ON_DURATION, DEFAULT_MIN_ON_DURATION
            ),
            CONF_MIN_OFF_DURATION: current.get(
                CONF_MIN_OFF_DURATION, DEFAULT_MIN_OFF_DURATION
            ),
            CONF_UPDATE_INTERVAL: current.get(
                CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
            ),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, suggested_values
            ),
        )
