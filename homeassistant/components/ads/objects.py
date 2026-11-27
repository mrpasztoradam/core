"""Proof of concept: TF8040 objects as devices with several entities.

A TF8040 object (Tc3_XBA FB_BA_BI, FB_BA_AV, ...) carries its present value
and its state as members, so one object becomes one device whose entities are
the present value and diagnostics of it. Writing through the priority array
is left out; commandable objects are read only here.
"""

from collections.abc import Iterable
from typing import Any
from uuid import uuid4

from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    Platform,
    UnitOfTemperature,
    UnitOfTime,
)

from .const import (
    CONF_ADS_TYPE,
    CONF_ADS_VAR,
    CONF_COMPONENTS,
    CONF_ENTITY_CATEGORY,
    CONF_MODEL,
    CONF_OBJECT,
    AdsType,
)

# E_BA_ObjectType values (Tc3_XBA) and what the object is called.
OBJECT_TYPES = {
    10: "Analog input",
    11: "Analog output",
    12: "Analog value",
    15: "Binary input",
    16: "Binary output",
    17: "Binary value",
    20: "Multistate input",
    21: "Multistate output",
    22: "Multistate value",
}
ANALOG = {10, 11, 12}
BINARY = {15, 16, 17}
MULTISTATE = {20, 21, 22}
# Inputs have no priority array.
COMMANDABLE = {11, 12, 16, 17, 21, 22}

# E_BA_Unit values (BACnet engineering units) Home Assistant has a unit for.
UNITS = {
    62: UnitOfTemperature.CELSIUS,
    64: UnitOfTemperature.FAHRENHEIT,
    71: UnitOfTime.HOURS,
    72: UnitOfTime.MINUTES,
    73: UnitOfTime.SECONDS,
    98: PERCENTAGE,
}
UNIT_DEVICE_CLASSES = {
    62: "temperature",
    64: "temperature",
    71: "duration",
    72: "duration",
    73: "duration",
}


class UnsupportedObjectError(Exception):
    """The object is not one a device can be made of."""


def object_components(
    path: str,
    object_type: int,
    unit: int | None,
    members: Iterable[str],
    *,
    name: str | None = None,
    device_class: str | None = None,
) -> list[dict[str, Any]]:
    """Return the entities of an object: its present value and diagnostics.

    The diagnostics are only added for members the object has; FB_BA_AV_Op
    and the other _Op variants have no out-of-service flag or priorities.
    """
    if object_type not in OBJECT_TYPES:
        raise UnsupportedObjectError(object_type)
    members = set(members)
    value: dict[str, Any]
    if object_type in BINARY:
        value = {
            CONF_PLATFORM: Platform.BINARY_SENSOR,
            CONF_ADS_VAR: f"{path}.bPresentValue",
        }
    elif object_type in ANALOG:
        value = {
            CONF_PLATFORM: Platform.SENSOR,
            CONF_ADS_VAR: f"{path}.fPresentValue",
            CONF_ADS_TYPE: AdsType.REAL,
        }
        if unit in UNITS:
            value[CONF_UNIT_OF_MEASUREMENT] = UNITS[unit]
        if unit is not None:
            device_class = device_class or UNIT_DEVICE_CLASSES.get(unit)
    else:
        # Multistate present values are 1-based state numbers.
        value = {
            CONF_PLATFORM: Platform.SENSOR,
            CONF_ADS_VAR: f"{path}.nPresentValue",
            CONF_ADS_TYPE: AdsType.UDINT,
        }
    if name:
        value[CONF_NAME] = name
    if device_class:
        value[CONF_DEVICE_CLASS] = device_class
    components = [value]
    prefix = f"{name} " if name else ""
    if "bEnOutOfService" in members:
        components.append(
            {
                CONF_PLATFORM: Platform.BINARY_SENSOR,
                CONF_ADS_VAR: f"{path}.bEnOutOfService",
                CONF_NAME: f"{prefix}out of service".capitalize(),
                CONF_ENTITY_CATEGORY: "diagnostic",
            }
        )
    if object_type in COMMANDABLE and "eActivePrio" in members:
        # E_BA_Priority: 3 means a remote client such as Home Assistant has it.
        components.append(
            {
                CONF_PLATFORM: Platform.SENSOR,
                CONF_ADS_VAR: f"{path}.eActivePrio",
                CONF_ADS_TYPE: AdsType.USINT,
                CONF_NAME: f"{prefix}active priority".capitalize(),
                CONF_ENTITY_CATEGORY: "diagnostic",
            }
        )
    return components


def object_device(path: str, info: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the title and subentry data of the device for an object."""
    object_type = info["object_type"]
    components = object_components(
        path, object_type, info.get("unit"), info.get("members", set())
    )
    title = info.get("description") or info.get("name") or path
    return title, {
        CONF_OBJECT: path,
        CONF_MODEL: f"TF8040 {OBJECT_TYPES[object_type].lower()}",
        CONF_COMPONENTS: {uuid4().hex: component for component in components},
    }
