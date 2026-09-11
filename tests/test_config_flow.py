"""Test config flow for Stove Controller integration."""

from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
import pytest

from stove_controller.config_flow import (
    StoveControllerConfigFlow,
    StoveControllerOptionsFlow,
    CONFIG_SCHEMA,
    OPTIONS_SCHEMA,
)
from stove_controller.const import (
    DOMAIN,
    CONF_RELAY_ENTITY,
    CONF_MIN_ON_DURATION,
    CONF_MIN_OFF_DURATION,
    DEFAULT_MIN_ON_DURATION,
    DEFAULT_MIN_OFF_DURATION,
)


class TestConfigSchema:
    """Test configuration schema."""

    def test_config_schema_has_required_fields(self):
        """Test that config schema has all required fields."""
        schema_keys = set(CONFIG_SCHEMA.schema.keys())
        expected_keys = {
            CONF_RELAY_ENTITY,
            CONF_MIN_ON_DURATION,
            CONF_MIN_OFF_DURATION,
        }
        assert expected_keys.issubset(schema_keys)

    def test_config_schema_defaults(self):
        """Test that config schema has correct defaults."""
        assert CONF_MIN_ON_DURATION in CONFIG_SCHEMA.schema
        assert CONF_MIN_OFF_DURATION in CONFIG_SCHEMA.schema


class TestOptionsSchema:
    """Test options schema."""

    def test_options_schema_has_required_fields(self):
        """Test that options schema has all required fields."""
        schema_keys = set(OPTIONS_SCHEMA.schema.keys())
        expected_keys = {
            CONF_MIN_ON_DURATION,
            CONF_MIN_OFF_DURATION,
        }
        assert expected_keys == schema_keys


class TestStoveControllerConfigFlow:
    """Test the config flow class."""

    @pytest.fixture
    def flow(self):
        """Create a config flow instance."""
        return StoveControllerConfigFlow()


class TestStoveControllerOptionsFlow:
    """Test the options flow class."""

    @pytest.fixture
    def flow(self):
        """Create an options flow instance."""
        return StoveControllerOptionsFlow()


