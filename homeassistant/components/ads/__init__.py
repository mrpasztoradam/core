"""Support for Automation Device Specification (ADS)."""

from collections.abc import Callable
import logging
from typing import Any

import probatio
import pyads

from homeassistant.const import (
    CONF_DEVICE,
    CONF_IP_ADDRESS,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

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

# Types whose PLC representation is not an integer. Everything else is written
# as one, so a value typed in the UI as text still reaches the PLC correctly.
ADS_VALUE_VALIDATORS: dict[AdsType, Callable[[Any], Any]] = {
    AdsType.BOOL: cv.boolean,
    AdsType.REAL: probatio.Coerce(float),
    AdsType.LREAL: probatio.Coerce(float),
    AdsType.STRING: cv.string,
}
DEFAULT_ADS_VALUE_VALIDATOR = probatio.Coerce(int)

CONF_ADS_FACTOR = "factor"
CONF_ADS_TYPE = "adstype"
CONF_ADS_VALUE = "value"


SERVICE_WRITE_DATA_BY_NAME = "write_data_by_name"

CONFIG_SCHEMA = probatio.Schema(
    {
        DOMAIN: probatio.Schema(
            {
                probatio.Required(CONF_DEVICE): cv.string,
                probatio.Required(CONF_PORT): cv.port,
                probatio.Optional(CONF_IP_ADDRESS): cv.string,
            }
        )
    },
    extra=probatio.ALLOW_EXTRA,
)


def _coerce_value_for_ads_type(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce the value to the Python type its ADS type is written as."""
    validator = ADS_VALUE_VALIDATORS.get(
        data[CONF_ADS_TYPE], DEFAULT_ADS_VALUE_VALIDATOR
    )
    return {**data, CONF_ADS_VALUE: validator(data[CONF_ADS_VALUE])}


SCHEMA_SERVICE_WRITE_DATA_BY_NAME = probatio.All(
    probatio.Schema(
        {
            probatio.Required(CONF_ADS_TYPE): probatio.Coerce(AdsType),
            probatio.Required(CONF_ADS_VALUE): probatio.Any(bool, int, float, str),
            probatio.Required(CONF_ADS_VAR): cv.string,
        }
    ),
    _coerce_value_for_ads_type,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the ADS component."""

    if (conf := config.get(DOMAIN)) is None:
        # Reachable without the section, because configuring any ads platform
        # pulls the component in.
        _LOGGER.error(
            "The ADS platforms need an '%s' section in configuration.yaml", DOMAIN
        )
        return False

    net_id = conf[CONF_DEVICE]
    ip_address = conf.get(CONF_IP_ADDRESS)
    port = conf[CONF_PORT]

    client = pyads.Connection(net_id, port, ip_address)

    try:
        ads = await hass.async_add_executor_job(AdsHub, client)
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
        value: bool | float | str = call.data[CONF_ADS_VALUE]

        await hass.async_add_executor_job(
            ads.write_by_name, ads_var, value, ADS_TYPEMAP[ads_type]
        )

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_WRITE_DATA_BY_NAME,
        handle_write_data_by_name,
        schema=SCHEMA_SERVICE_WRITE_DATA_BY_NAME,
    )

    return True
