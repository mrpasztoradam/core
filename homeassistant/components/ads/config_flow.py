"""Config flow for Automation Device Specification (ADS)."""

import pyads
import voluptuous as vol

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    DOMAIN as SENSOR_DOMAIN,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_NAME,
    CONF_UNIT_OF_MEASUREMENT,
    UnitOfTemperature,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from . import ADS_TYPEMAP, CONF_ADS_TYPE
from .const import CONF_ADS_FACTOR, CONF_ADS_VAR, DOMAIN
from .hub import AdsHub

# Define configuration keys
CONF_DEVICE = "device"
CONF_PORT = "port"
CONF_IP_ADDRESS = "ip_address"


SENSOR_SETUP = {
    vol.Required(CONF_ADS_VAR): TextSelector(),
    vol.Optional(CONF_ADS_FACTOR, default=1): NumberSelector(
        NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX)
    ),
    vol.Optional(CONF_ADS_TYPE): SelectSelector(
        SelectSelectorConfig(
            options=[cls.value for cls in ADS_TYPEMAP],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="state_class",
            sort=True,
        ),
    ),
    vol.Optional(CONF_NAME): TextSelector(),
    vol.Optional(CONF_DEVICE_CLASS): SelectSelector(
        SelectSelectorConfig(
            options=[
                cls.value for cls in SensorDeviceClass if cls != SensorDeviceClass.ENUM
            ],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="device_class",
            sort=True,
        )
    ),
    vol.Optional(CONF_STATE_CLASS): SelectSelector(
        SelectSelectorConfig(
            options=[cls.value for cls in SensorStateClass],
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="state_class",
            sort=True,
        )
    ),
    vol.Optional(CONF_UNIT_OF_MEASUREMENT): SelectSelector(
        SelectSelectorConfig(
            options=[cls.value for cls in UnitOfTemperature],
            custom_value=True,
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="unit_of_measurement",
            sort=True,
        )
    ),
}


DATA_SCHEMA_HUB = vol.Schema(
    {
        vol.Required(CONF_DEVICE, default=""): str,
        vol.Required(CONF_PORT, default=851): cv.port,
        vol.Optional(CONF_IP_ADDRESS, default=""): str,
    }
)

DATA_SCHEMA_SENSOR = vol.Schema(
    {
        vol.Optional(CONF_NAME, default="Sensor"): TextSelector(),
        **SENSOR_SETUP,
    }
)


class ADSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ADS."""

    VERSION = 0
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return ADSOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Check if device (AMS Net ID) is provided and valid
            if not user_input.get(CONF_DEVICE) or not self._is_valid_ams_net_id(
                user_input[CONF_DEVICE]
            ):
                errors[CONF_DEVICE] = (
                    "invalid_ams_net_id" if user_input.get(CONF_DEVICE) else "required"
                )

            # Check if port is in valid range
            if not (1 <= user_input.get(CONF_PORT, 0) <= 65535):
                errors[CONF_PORT] = (
                    "invalid_port" if user_input.get(CONF_PORT) else "required"
                )

            # Test the connection if no validation errors exist
            if not errors:
                # Create a temporary pyads connection client
                ads_client = pyads.Connection(
                    user_input[CONF_DEVICE],
                    user_input[CONF_PORT],
                    user_input.get(CONF_IP_ADDRESS),
                )
                hub = AdsHub(ads_client)

                mac_address = hub.get_mac_address()

                if mac_address is None:
                    raise ValueError("Failed to retrieve MAC address")

                # Check if this MAC address already exists in existing config entries
                for entry in self._async_current_entries():
                    if entry.data.get("mac_address") == mac_address:
                        errors["base"] = "duplicate_mac"
                        break
                # Test the connection
                await self.hass.async_add_executor_job(hub.test_connection)

            if not errors:
                # If validation passes, create entry
                return self.async_create_entry(
                    title=f"ADS Device ({mac_address})",
                    data={
                        CONF_DEVICE: user_input[CONF_DEVICE],
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_IP_ADDRESS: user_input.get(CONF_IP_ADDRESS),
                        "mac_address": mac_address,
                    },
                )

        return self.async_show_form(
            data_schema=DATA_SCHEMA_HUB,
            errors=errors,
        )

    def _is_valid_ams_net_id(self, net_id):
        """Check if AMS net ID is in correct format (like '192.168.10.120.1.1'), with all parts between 0 and 255."""
        parts = net_id.split(".")

        if len(parts) != 6:
            return False

        try:
            return all(0 <= int(part) <= 255 for part in parts)
        except ValueError:
            return False


class ADSOptionsFlowHandler(OptionsFlow):
    """Handles the options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        # self.config_entry = config_entry
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Handle options flow."""

        if user_input is not None:
            if user_input.get("add_entity"):
                return await self.async_step_add_entity()

            if user_input.get("hub_settings"):
                return await self.async_step_hub_settings()

        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "hub_settings",
                "add_entity",
                "modify_entity",
                "remove_entity",
            ],
        )

    async def async_step_hub_settings(self, user_input=None) -> ConfigFlowResult:
        """Handle menu hub settings flow."""
        if user_input is not None:
            options = self.config_entry.options | user_input
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=options
            )
            return self.async_create_entry(title="Updated Hub Settings", data=options)

        return self.async_show_form(
            step_id="hub_settings",
            data_schema=DATA_SCHEMA_HUB,
        )

    async def async_step_add_entity(self, user_input=None) -> ConfigFlowResult:
        """Handle adding a new entity (select entity type)."""
        if user_input is not None:
            entity_type = user_input.get("entity_type")
            if entity_type == "sensor":
                return await self.async_step_add_sensor()
            if entity_type == "cover":
                return await self.async_step_add_cover()

        return self.async_show_menu(
            step_id="add_entity",
            menu_options=[
                "add_sensor",
                "add_cover",
            ],
        )

    async def async_step_add_sensor(self, user_input=None) -> ConfigFlowResult:
        """Handle sensor entity configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate and save sensor configuration here
            sensor = user_input
            # Add the new sensor configuration to options
            sensors = self.options.get("sensors", [])
            sensors.append(sensor)
            self.options["sensors"] = sensors

            # Save updated options
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=self.options
            )

            # Forward the config entry setup to the sensor domain
            await self.hass.config_entries.async_forward_entry_setups(
                self.config_entry, [SENSOR_DOMAIN]
            )

            return self.async_create_entry(title="New Sensor", data=self.options)

        # Return form for configuring the sensor entity
        return self.async_show_form(
            step_id="add_sensor",
            data_schema=DATA_SCHEMA_SENSOR,
            errors=errors,
        )

    async def async_step_add_cover(self, user_input=None) -> ConfigFlowResult:
        """Handle cover entity configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Handle cover entity configuration here
            cover_options = user_input
            self.options[CONF_NAME] = cover_options.get(CONF_NAME)

            # Optionally add more validation and configuration logic for covers

            # Save the configuration
            self.hass.config_entries.async_update_entry(
                self.config_entry, options=self.options
            )

            return self.async_create_entry(title="New Cover", data=self.options)

        # Return form for configuring the cover entity
        return self.async_show_form(
            step_id="add_cover",
            data_schema=vol.Schema(
                {vol.Required(CONF_NAME): TextSelector()}
            ),  # Example for cover
            errors=errors,
        )
