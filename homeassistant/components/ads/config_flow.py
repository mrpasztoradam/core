"""Config flow for Automation Device Specification (ADS)."""

import logging

import voluptuous as vol

from homeassistant.components.sensor import (
    DEVICE_CLASSES_SCHEMA as SENSOR_DEVICE_CLASSES_SCHEMA,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_DEVICE_CLASS, CONF_UNIT_OF_MEASUREMENT
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import CONF_ADS_TYPE, CONF_ADS_VAR, DOMAIN, AdsType

_LOGGER = logging.getLogger(__name__)

# Define configuration keys
CONF_DEVICE = "device"
CONF_PORT = "port"
CONF_IP_ADDRESS = "ip_address"
CONF_ENTITY_TYPE = "entity_type"
CONF_ADS_FACTOR = "ads_factor"
# CONF_ADS_TYPE = "ads_type"
CONF_NAME = "name"

# Entity types (sensor, valve, switch, etc.)
ENTITY_TYPES = ["sensor", "switch", "valve"]


class ADSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ADS."""

    VERSION = 0
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.entity_type: str | None = None
        self.entity_data: dict | None = None

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        # Check if a hub entry already exists
        existing_entries = [
            entry for entry in self._async_current_entries() if entry.data.get("device")
        ]
        if existing_entries:
            # Hub is already configured; proceed to add a new entity
            return await self.async_step_entity_selection()

        if user_input is not None:
            # Store the initial ADS hub data and create the config entry
            return self.async_create_entry(
                title="ADS Hub",
                data={
                    CONF_DEVICE: user_input[CONF_DEVICE],
                    CONF_PORT: user_input[CONF_PORT],
                    CONF_IP_ADDRESS: user_input.get(CONF_IP_ADDRESS),
                },
            )

        # Define the input schema with required fields for config flow form
        data_schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=""): str,
                vol.Required(CONF_PORT, default=851): cv.port,
                vol.Optional(CONF_IP_ADDRESS, default=""): str,
            }
        )

        # Show the form with schema and errors (if any)
        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow to add entities like sensors or switches."""
        return ADSOptionsFlowHandler(config_entry)

    def _is_valid_ams_net_id(self, net_id):
        """Check if AMS net ID is in correct format (like '192.168.10.120.1.1'), with all parts between 0 and 255."""
        parts = net_id.split(".")

        if len(parts) != 6:
            return False

        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False

    async def async_step_entity_selection(self, user_input=None) -> ConfigFlowResult:
        """Step to select entity type (sensor, switch, etc.) after hub is configured."""
        errors: dict[str, str] = {}

        # Schema for selecting entity type
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_TYPE): vol.In(ENTITY_TYPES),
            }
        )

        if user_input is not None:
            # Move to configuration for the specific entity type
            self.entity_type = user_input[CONF_ENTITY_TYPE]
            if self.entity_type == "sensor":
                return await self.async_step_sensor()
            if self.entity_type == "switch":
                return await self.async_step_switch()

        return self.async_show_form(
            step_id="entity_selection",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_sensor(self, user_input=None) -> ConfigFlowResult:
        """Step to configure an individual sensor entity."""
        errors: dict[str, str] = {}

        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADS_VAR): str,
                vol.Optional(CONF_ADS_TYPE, default=AdsType.INT): vol.In(AdsType),
                vol.Optional(CONF_NAME, default="ADS Sensor"): str,
                vol.Optional(CONF_ADS_FACTOR): int,
                vol.Optional(CONF_DEVICE_CLASS): SENSOR_DEVICE_CLASSES_SCHEMA,
                vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
            }
        )

        if user_input is not None:
            # Create the entry for this entity
            return self.async_create_entry(
                title=user_input.get(
                    CONF_NAME, "ADS Sensor"
                ),  # Use a dynamic title if needed
                data={
                    "entity_type": "sensor",
                    CONF_ADS_VAR: user_input[CONF_ADS_VAR],
                    CONF_ADS_TYPE: AdsType[user_input[CONF_ADS_TYPE].upper()],
                    CONF_NAME: user_input.get(CONF_NAME, "ADS Sensor"),
                    CONF_ADS_FACTOR: user_input.get(CONF_ADS_FACTOR),
                    CONF_DEVICE_CLASS: user_input.get(CONF_DEVICE_CLASS),
                    CONF_UNIT_OF_MEASUREMENT: user_input.get(CONF_UNIT_OF_MEASUREMENT),
                },
            )

        # Show the sensor configuration form
        return self.async_show_form(
            step_id="sensor", data_schema=data_schema, errors=errors
        )

    async def async_step_switch(self, user_input=None) -> ConfigFlowResult:
        """Step to configure an individual switch entity."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Save the switch configuration as a new config entry for the switch
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, "ADS Switch"),
                data={
                    CONF_ADS_VAR: user_input[CONF_ADS_VAR],
                    CONF_NAME: user_input.get(CONF_NAME, "ADS switch"),
                    "entity_type": "switch",
                },
            )

        # Define schema for switch configuration
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADS_VAR): str,
                vol.Optional(CONF_NAME, default="ADS switch"): str,
            }
        )

        return self.async_show_form(
            step_id="switch",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_remove_entity(self, user_input=None) -> ConfigFlowResult:
        """Handle entity removal."""

        if user_input:
            entity_id = user_input["entity_id"]
            _LOGGER.info("Removing entity %s", entity_id)

            # Get the entity registry and remove the specific entity
            entity_registry = er.async_get(self.hass)
            entity_registry.async_remove(entity_id)

            # Return to show that the entity has been removed
            return self.async_create_entry(title="Entity Removed", data={})

        # If no user_input yet, show a list of entities to choose from
        entities = self.get_entities_to_remove()
        return self.async_show_form(
            step_id="remove_entity",
            data_schema=vol.Schema({vol.Required("entity_id"): vol.In(entities)}),
        )

    # Helper function to get removable entities
    def get_entities_to_remove(self):
        """Get a list of entities that can be removed for this integration."""
        entity_registry = er.async_get(self.hass)
        return {
            entity.entity_id: entity.original_name or entity.entity_id
            for entity in entity_registry.entities.values()
            if entity.platform == "ads"
        }


class ADSOptionsFlowHandler(OptionsFlow):
    """Handle the options flow for ADS integration."""

    def __init__(self, config_entry) -> None:
        """Initialize ADS options flow."""
        self.config_entry = config_entry
        self.entity_type = None

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Manage the options for the ADS integration."""

        errors: dict[str, str] = {}

        if user_input is not None:
            # Store entity type and move to the entity configuration step
            self.entity_type = user_input[CONF_ENTITY_TYPE]
            if self.entity_type == "sensor":
                return await self.async_step_sensor()
            if self.entity_type == "switch":
                return await self.async_step_switch()

        # Schema for selecting entity type
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ENTITY_TYPE): vol.In(ENTITY_TYPES),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_sensor(self, user_input=None) -> ConfigFlowResult:
        """Step to configure options for adding an ADS sensor."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Save the sensor configuration in the entry options
            new_options = {
                **self.config_entry.options,
                "sensor": {
                    CONF_ADS_VAR: user_input[CONF_ADS_VAR],
                    CONF_ADS_FACTOR: user_input.get(CONF_ADS_FACTOR),
                    CONF_ADS_TYPE: user_input.get(CONF_ADS_TYPE),
                    CONF_NAME: user_input.get(CONF_NAME, "ADS sensor"),
                    CONF_DEVICE_CLASS: user_input.get(CONF_DEVICE_CLASS),
                    CONF_UNIT_OF_MEASUREMENT: user_input.get(CONF_UNIT_OF_MEASUREMENT),
                },
            }
            return self.async_create_entry(title="Add Sensor", data=new_options)

        # Define schema for sensor configuration
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADS_VAR): str,
                vol.Optional(CONF_ADS_FACTOR): vol.Coerce(int),
                vol.Optional(CONF_ADS_TYPE, default=AdsType.INT): vol.In(AdsType),
                vol.Optional(CONF_NAME, default="ADS sensor"): str,
                vol.Optional(CONF_DEVICE_CLASS): SENSOR_DEVICE_CLASSES_SCHEMA,
                vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
            }
        )

        return self.async_show_form(
            step_id="sensor",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_switch(self, user_input=None) -> ConfigFlowResult:
        """Step to configure options for adding an ADS switch."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Save the switch configuration in the entry options
            new_options = {
                **self.config_entry.options,
                "switch": {
                    CONF_ADS_VAR: user_input[CONF_ADS_VAR],
                    CONF_NAME: user_input.get(CONF_NAME, "ADS switch"),
                },
            }
            return self.async_create_entry(title="Add Switch", data=new_options)

        # Define schema for switch configuration
        data_schema = vol.Schema(
            {
                vol.Required(CONF_ADS_VAR): str,
                vol.Optional(CONF_NAME, default="ADS switch"): str,
            }
        )

        return self.async_show_form(
            step_id="switch",
            data_schema=data_schema,
            errors=errors,
        )
