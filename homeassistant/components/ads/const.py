"""Support for Automation Device Specification (ADS)."""

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING

import pyads

from homeassistant.const import Platform
from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .hub import AdsHub

DOMAIN = "ads"

# The hub of an unloaded entry, kept as the registry of the YAML entities that
# outlive it, so they can be rebound once the entry is set up again.
DATA_ADS: HassKey[AdsHub] = HassKey(DOMAIN)

# Set once the entry's latest setup attempt finished, loaded or not, so the
# YAML platforms can wait for it instead of retrying on a timer of their own.
DATA_SETUP_DONE: HassKey[asyncio.Event] = HassKey(f"{DOMAIN}_setup_done")

DEFAULT_PORT = 851

CONF_ADS_VAR = "adsvar"

STATE_KEY_STATE = "state"


class AdsType(StrEnum):
    """Supported Types."""

    BOOL = "bool"
    BYTE = "byte"
    INT = "int"
    UINT = "uint"
    SINT = "sint"
    USINT = "usint"
    DINT = "dint"
    UDINT = "udint"
    WORD = "word"
    DWORD = "dword"
    LREAL = "lreal"
    REAL = "real"
    STRING = "string"
    TIME = "time"
    DATE = "date"
    DATE_AND_TIME = "dt"
    TOD = "tod"


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

# A subentry is a device whose entities are its components, each for one
# platform, as MQTT models them.
SUBENTRY_TYPE_DEVICE = "device"
CONF_COMPONENTS = "components"
# The platforms a device's entity can be created for in the UI.
SUBENTRY_PLATFORMS = [Platform.SWITCH]

# Symbol types the PLC reports for a variable a switch can write.
BOOL_SYMBOL_TYPES = frozenset({"BOOL", "BIT"})
# Elementary IEC 61131-3 types the PLC reports. A variable of one of these is
# known not to be a BOOL; anything else, such as an alias or a structure, may
# still be one.
ELEMENTARY_SYMBOL_TYPES = frozenset(
    {ads_type.upper() for ads_type in AdsType}
    | {
        "DATE_AND_TIME",
        "LINT",
        "LTIME",
        "LWORD",
        "TIME_OF_DAY",
        "ULINT",
        "WSTRING",
    }
)
