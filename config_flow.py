"""Config flow for the Stove Controller integration."""

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
    DEFAULT_MIN_OFF_DURATION,
    DEFAULT_MIN_ON_DURATION,
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
                min=1, max=180, step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(
            CONF_MIN_OFF_DURATION, default=DEFAULT_MIN_OFF_DURATION
        ): NumberSelector(
            NumberSelectorConfig(
                min=1, max=180, step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MIN_ON_DURATION): NumberSelector(
            NumberSelectorConfig(
                min=1, max=180, step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_MIN_OFF_DURATION): NumberSelector(
            NumberSelectorConfig(
                min=1, max=180, step=1,
                unit_of_measurement="min",
                mode=NumberSelectorMode.BOX,
            )
        ),
    }
)


class StoveControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Stove Controller."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(
                title="Stove Controller", data=user_input
            )
        return self.async_show_form(
            step_id="user", data_schema=CONFIG_SCHEMA
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow."""
        return StoveControllerOptionsFlow()


class StoveControllerOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle options flow with automatic reload on change."""

    async def async_step_init(self, user_input=None):
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        suggested_values = {
            CONF_MIN_ON_DURATION: current.get(
                CONF_MIN_ON_DURATION, DEFAULT_MIN_ON_DURATION
            ),
            CONF_MIN_OFF_DURATION: current.get(
                CONF_MIN_OFF_DURATION, DEFAULT_MIN_OFF_DURATION
            ),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, suggested_values
            ),
        )
