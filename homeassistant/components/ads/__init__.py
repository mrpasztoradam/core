"""Support for Automation Device Specification (ADS)."""

from collections.abc import Mapping
import logging
from typing import Any

import pyads
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE,
    CONF_IP_ADDRESS,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, ServiceCall
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .config_flow import ADSOptionsFlowHandler
from .const import CONF_ADS_VAR, DATA_ADS, DOMAIN, AdsType
from .hub import AdsHub

_LOGGER = logging.getLogger(__name__)

ADS_TYPEMAP = {
    AdsType.BOOL: pyads.PLCTYPE_BOOL,
    AdsType.BYTE: pyads.PLCTYPE_BYTE,
    AdsType.INT: pyads.PLCTYPE_INT,
    AdsType.UINT: pyads.PLCTYPE_UINT,
    AdsType.SINT: pyads.PLCTYPE_SINT,
    AdsType.USINT: pyads.PLCTYPE_USINT,
    AdsType.DINT: pyads.PLCTYPE_DINT,
    AdsType.UDINT: pyads.PLCTYPE_UDINT,
    AdsType.WORD: pyads.PLCTYPE_WORD,
    AdsType.DWORD: pyads.PLCTYPE_DWORD,
    AdsType.REAL: pyads.PLCTYPE_REAL,
    AdsType.LREAL: pyads.PLCTYPE_LREAL,
    AdsType.STRING: pyads.PLCTYPE_STRING,
    AdsType.TIME: pyads.PLCTYPE_TIME,
    AdsType.DATE: pyads.PLCTYPE_DATE,
    AdsType.DATE_AND_TIME: pyads.PLCTYPE_DT,
    AdsType.TOD: pyads.PLCTYPE_TOD,
}

CONF_ADS_FACTOR = "factor"
CONF_ADS_TYPE = "adstype"
CONF_ADS_VALUE = "value"
SERVICE_WRITE_DATA_BY_NAME = "write_data_by_name"

# YAML Configuration Schema (to allow setup from configuration.yaml)
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_DEVICE): cv.string,
                vol.Required(CONF_PORT): cv.port,
                vol.Optional(CONF_IP_ADDRESS): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the ADS component with optional YAML configuration."""
    # Check if YAML configuration exists for this domain
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]
    # Store the AdsHub instance in hass.data[DOMAIN]
    return await async_setup_ads_integration(hass, conf)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ADS hub from a config entry via the GUI."""

    if CONF_DEVICE in entry.data:
        # This is the hub configuration
        return await async_setup_ads_integration(hass, entry.data)
    if "entity_type" in entry.data:
        # This is an individual entity (e.g., sensor, switch)
        return await async_setup_ads_entity(hass, entry)
    # If entry doesn't match hub or entity format, log an error
    _LOGGER.error("Unknown entry format for ADS integration: %s", entry.data)
    return False


async def async_setup_ads_entity(hass, entry):
    """Set up an individual ADS entity from a config entry."""
    entity_type = entry.data.get("entity_type")
    if not entity_type:
        _LOGGER.error("Missing entity type in ADS entity setup")
        return False

    # Setup logic for the individual entity (e.g., sensor, switch)
    # Based on the entity_type (sensor, switch), initiate the correct platform
    if entity_type == "sensor":
        _LOGGER.debug("Forwarding setup to the sensor platform")
        await hass.components.sensor.async_setup_entry(hass, entry)
    elif entity_type == "switch":
        _LOGGER.debug("Forwarding setup to the switch platform")
        await hass.components.switch.async_setup_entry(hass, entry)
    else:
        _LOGGER.error("Unsupported entity type for ADS integration: %s", entity_type)
        return False

    return True


async def async_setup_ads_integration(
    hass: HomeAssistant, config: Mapping[str, Any]
) -> bool:
    """Set up common components for both YAML and config entry setups."""
    net_id = config[CONF_DEVICE]
    ip_address = config.get(CONF_IP_ADDRESS)
    port = config[CONF_PORT]

    client = pyads.Connection(net_id, port, ip_address)

    try:
        ads = AdsHub(client)
    except pyads.ADSError:
        _LOGGER.error(
            "Could not connect to ADS host (netid=%s, ip=%s, port=%s)",
            net_id,
            ip_address,
            port,
        )
        return False

    hass.data[DATA_ADS] = ads
    hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, ads.shutdown)

    async def handle_write_data_by_name(call: ServiceCall) -> None:
        """Write a value to the connected ADS device."""
        ads_var: str = call.data[CONF_ADS_VAR]
        ads_type: AdsType = call.data[CONF_ADS_TYPE]
        value: int = call.data[CONF_ADS_VALUE]

        try:
            ads.write_by_name(ads_var, value, ADS_TYPEMAP[ads_type])
        except pyads.ADSError as err:
            _LOGGER.error(err)

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_DATA_BY_NAME,
        handle_write_data_by_name,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the ADS entry."""
    _LOGGER.debug("Unloading ADS entry: %s", entry.entry_id)

    # Check if the ADS data exists in hass.data
    ads_data = hass.data.get(DATA_ADS, None)
    if ads_data is None:
        _LOGGER.warning(
            "No ADS data found in hass.data during unload for entry: %s", entry.entry_id
        )
        return False  # Return False if no data is found, indicating failure

    _LOGGER.debug("Found ADS data, proceeding to shutdown")

    if len(ads_data._devices) > 0:  # Access _entities directly  # noqa: SLF001
        _LOGGER.debug("Entities are still present, not unloading ADS hub yet")
        return False  # Return False if entities are still present

    try:
        if hasattr(ads_data, "shutdown"):
            ads_data.shutdown()  # Shutdown the connection if it exists
            _LOGGER.debug("ADS connection shut down successfully")
        else:
            _LOGGER.error(
                "No shutdown method available on ADS data for entry: %s", entry.entry_id
            )
            return False  # Return False if no shutdown method is available
    except pyads.ADSError as e:
        _LOGGER.error("Error during shutdown of ADS connection: %s", e)
        return False  # Return False if an ADS-specific error occurs during shutdown
    except Exception as e:  # noqa: BLE001
        _LOGGER.error("Unexpected error during shutdown: %s", e)
        return False  # Return False if an unexpected error occurs during shutdown

    # Clean up the data by deleting it from hass.data
    del hass.data[DATA_ADS]
    return True  # Return True if the unload was successful


async def async_get_options_flow(config_entry):
    """Get the options flow for this handler."""
    return ADSOptionsFlowHandler(config_entry)
