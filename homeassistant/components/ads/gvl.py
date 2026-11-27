"""Proof of concept: devices from the variables of a GVL.

A GVL (global variable list) is read from the PLC's symbol table, and its
variables' types and comments from the data type table:

- A struct variable (HA.Living) becomes a device named after it, with an
  entity per elementary member.
- A struct member marked "ADS <platform>: <option>" in its members' comments
  (ST_HA_Light: "ADS light: adsvar", "ADS light: adsvar_brightness") becomes
  a device of that platform. A struct with the members of a cover (bClosed,
  nPosition, nSetPosition, bOpen, bClose, bStop) becomes a cover.
- The elementary variables of the GVL itself (IO.Bath_Btn1A) become one device
  named after the GVL.

A member's comment says what it is: "command: ..." is written by Home
Assistant (a button when "reset by the PLC", a switch for a BOOL, a number
otherwise), "command and state" is a switch, anything else is read only. A
unit in brackets ("state [°C]") gives the unit and device class.
"""

from ctypes import create_string_buffer
from dataclasses import dataclass, field
from functools import partial
import re
import struct
from typing import Any
from uuid import uuid4

import pyads
from pyads.constants import (
    ADSIGRP_SYM_DT_UPLOAD,
    ADSIGRP_SYM_UPLOADINFO2,
    ADSIOFFS_DEVDATA_ADSSTATE,
    PLCTYPE_STRING,
)

from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_UNIT_OF_MEASUREMENT,
    Platform,
)

from .const import (
    CONF_ADS_TYPE,
    CONF_ADS_VAR,
    CONF_AREA,
    CONF_COMPONENTS,
    CONF_MODEL,
    AdsType,
)

# PLC type names an entity can be made of, and the ADS type they are read as.
ELEMENTARY = {ads_type.upper(): ads_type for ads_type in AdsType}
NUMERIC = set(ELEMENTARY) - {"BOOL", "STRING", "TIME", "DATE", "DT", "TOD"}
ARRAY = re.compile(r"ARRAY \[(-?\d+)\.\.(-?\d+)\] OF (.+)")
MAX_ARRAY = 16
MARKER = re.compile(r"ADS (\w+): (\w+)")
UNIT = re.compile(r"\[([^\]]+)\]")
# Units Home Assistant knows, and the device class they imply.
UNITS = {
    "°C": ("°C", "temperature"),
    "W": ("W", "power"),
    "kW": ("kW", "power"),
    "kWh": ("kWh", "energy"),
    "V": ("V", "voltage"),
    "A": ("A", "current"),
    "Hz": ("Hz", "frequency"),
    "%": ("%", None),
    "K": ("K", None),
    "s": ("s", "duration"),
    "ms": ("ms", "duration"),
}
COVER_MEMBERS = {
    "bClosed": CONF_ADS_VAR,
    "nPosition": "adsvar_position",
    "nSetPosition": "adsvar_set_position",
    "bOpen": "adsvar_open",
    "bClose": "adsvar_close",
    "bStop": "adsvar_stop",
}
BINARY_CLASSES = [
    ("window", "window"),
    ("presence", "occupancy"),
    ("door", "door"),
    ("err", "problem"),
    ("fault", "problem"),
    ("fail", "problem"),
]


@dataclass
class GvlDevice:
    """A device to add for a variable of the GVL."""

    key: str
    title: str
    area: str | None
    components: list[dict[str, Any]] = field(default_factory=list)


def _decode(raw: bytes) -> str:
    return " ".join(raw.decode("utf-8", errors="replace").strip("\0").split())


def _parse(buf: bytes, ptr: int) -> tuple[dict[str, Any], int]:
    entry_len = struct.unpack_from("<I", buf, ptr)[0]
    name_len, type_len, comment_len, array_dims, sub_count = struct.unpack_from(
        "<HHHHH", buf, ptr + 32
    )
    pos = ptr + 42
    name = _decode(buf[pos : pos + name_len])
    pos += name_len + 1
    type_name = _decode(buf[pos : pos + type_len])
    pos += type_len + 1
    comment = _decode(buf[pos : pos + comment_len])
    pos += comment_len + 1 + 8 * array_dims
    subs = []
    for _ in range(sub_count):
        sub, pos = _parse(buf, pos)
        subs.append(sub)
    return {"name": name, "type": type_name, "comment": comment, "sub": subs}, (
        ptr + entry_len
    )


def read_layout(
    client: pyads.Connection,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str]]]:
    """Read the data type table and the symbols (name, type, comment)."""
    info = bytes(
        client.read(
            ADSIGRP_SYM_UPLOADINFO2,
            ADSIOFFS_DEVDATA_ADSSTATE,
            PLCTYPE_STRING,
            return_ctypes=True,
        )
    )
    count, length = struct.unpack_from("<II", info, 8)
    buf = bytes(
        client.read(
            ADSIGRP_SYM_DT_UPLOAD,
            ADSIOFFS_DEVDATA_ADSSTATE,
            partial(create_string_buffer, length),
            return_ctypes=True,
        )
    )
    types: dict[str, dict[str, Any]] = {}
    ptr = 0
    for _ in range(count):
        entry, ptr = _parse(buf, ptr)
        types[entry["name"]] = entry
    symbols = [
        (symbol.name, symbol.symbol_type, symbol.comment or "")
        for symbol in client.get_all_symbols()
    ]
    return types, symbols


def list_gvls(symbols: list[tuple[str, str, str]]) -> dict[str, int]:
    """Return the GVLs and how many variables each has."""
    gvls: dict[str, int] = {}
    for name, _, _ in symbols:
        if "." in name:
            gvl = name.split(".", 1)[0]
            gvls[gvl] = gvls.get(gvl, 0) + 1
    return dict(sorted(gvls.items(), key=lambda item: item[0].lower()))


def humanize(name: str) -> str:
    """Turn bWindowOpen or Bath_CeilLed into "Window open" or "Bath ceil led"."""
    name = re.sub(r"^(st|a|b|f|n|s|e|i)(?=[A-Z])", "", name)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", name).split()
    text = " ".join(words).lower()
    return text[:1].upper() + text[1:]


def _entity(
    path: str, member: str, type_name: str, comment: str
) -> dict[str, Any] | None:
    """Return the entity of an elementary variable, or None if it has none."""
    type_name = type_name.upper()
    base = "STRING" if type_name.startswith("STRING") else type_name
    if base not in ELEMENTARY:
        return None
    text = comment.lower()
    command = text.startswith("command")
    both = "command and state" in text
    component: dict[str, Any] = {CONF_ADS_VAR: path, CONF_NAME: humanize(member)}
    if base == "BOOL":
        if command and "reset by the plc" in text and not both:
            component[CONF_PLATFORM] = Platform.BUTTON
        elif command or both:
            component[CONF_PLATFORM] = Platform.SWITCH
        else:
            component[CONF_PLATFORM] = Platform.BINARY_SENSOR
            lowered = member.lower()
            for word, device_class in BINARY_CLASSES:
                if word in lowered:
                    component[CONF_DEVICE_CLASS] = device_class
                    break
        return component
    component[CONF_ADS_TYPE] = ELEMENTARY[base]
    component[CONF_PLATFORM] = (
        Platform.NUMBER if command and base in NUMERIC else Platform.SENSOR
    )
    if (match := UNIT.search(comment)) and match.group(1) in UNITS:
        unit, unit_class = UNITS[match.group(1)]
        component[CONF_UNIT_OF_MEASUREMENT] = unit
        if unit_class and component[CONF_PLATFORM] == Platform.SENSOR:
            component[CONF_DEVICE_CLASS] = unit_class
            if unit_class == "energy":
                component["state_class"] = "total_increasing"
        if unit == "%" and "humid" in member.lower():
            component[CONF_DEVICE_CLASS] = "humidity"
    return component


def _special(path: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Return the one entity a struct makes, if it is marked or a cover."""
    members = {sub["name"]: sub for sub in entry["sub"]}
    marked = {
        sub["name"]: match
        for sub in entry["sub"]
        if (match := MARKER.search(sub.get("comment", "")))
    }
    if marked:
        platform = next(iter(marked.values())).group(1)
        component: dict[str, Any] = {CONF_PLATFORM: platform}
        for member, match in marked.items():
            component[match.group(2)] = f"{path}.{member}"
        return component if CONF_ADS_VAR in component else None
    if "bClosed" in members and len(set(members) & set(COVER_MEMBERS)) >= 3:
        component = {CONF_PLATFORM: Platform.COVER, CONF_DEVICE_CLASS: "shutter"}
        for member, option in COVER_MEMBERS.items():
            if member in members:
                component[option] = f"{path}.{member}"
        return component
    return None


def _expand(name: str, type_name: str) -> list[tuple[str, str]]:
    """Return the variables an array stands for, or the variable itself."""
    if match := ARRAY.fullmatch(type_name):
        low, high, element = int(match[1]), int(match[2]), match[3]
        if high - low + 1 > MAX_ARRAY:
            return []
        return [(f"{name}[{index}]", element) for index in range(low, high + 1)]
    return [(name, type_name)]


def _nested_device(
    path: str, member: dict[str, Any], entry: dict[str, Any], area: str | None
) -> GvlDevice | None:
    """Return the device of a struct member of a struct variable."""
    device = GvlDevice(
        key=path, title=member.get("comment") or humanize(member["name"]), area=area
    )
    if special := _special(path, entry):
        device.components.append(special)
        return device
    for leaf in entry["sub"]:
        if component := _entity(
            f"{path}.{leaf['name']}",
            leaf["name"],
            leaf["type"],
            leaf.get("comment", ""),
        ):
            device.components.append(component)
    return device if device.components else None


def plan_gvl(
    gvl: str,
    types: dict[str, dict[str, Any]],
    symbols: list[tuple[str, str, str]],
) -> list[GvlDevice]:
    """Return the devices the variables of a GVL make."""
    loose = GvlDevice(key=gvl, title=gvl, area=None)
    devices: list[GvlDevice] = []
    for name, type_name, comment in symbols:
        if not name.startswith(f"{gvl}."):
            continue
        variable = name.split(".", 1)[1]
        entry = types.get(type_name)
        if entry is None or not entry["sub"]:
            for path, element in _expand(name, type_name):
                member = path.split(".", 1)[1]
                if component := _entity(path, member, element, comment):
                    loose.components.append(component)
            continue
        if component := _special(name, entry):
            devices.append(
                GvlDevice(name, comment or humanize(variable), None, [component])
            )
            continue
        device = GvlDevice(key=name, title=humanize(variable), area=humanize(variable))
        devices.append(device)
        for sub in entry["sub"]:
            sub_path = f"{name}.{sub['name']}"
            sub_entry = types.get(sub["type"])
            if sub_entry is not None and sub_entry["sub"]:
                # A struct in the struct: its own device in the same area.
                if nested := _nested_device(sub_path, sub, sub_entry, device.area):
                    devices.append(nested)
                continue
            for path, element in _expand(sub_path, sub["type"]):
                member = path.rsplit(".", 1)[1]
                if component := _entity(path, member, element, sub.get("comment", "")):
                    device.components.append(component)
    if loose.components:
        devices.append(loose)
    for device in devices:
        if len(device.components) == 1:
            # A device of one entity names it.
            device.components[0].pop(CONF_NAME, None)
    return [device for device in devices if device.components]


def device_data(device: GvlDevice, with_area: bool) -> dict[str, Any]:
    """Return the subentry data of a device."""
    data: dict[str, Any] = {
        CONF_MODEL: "GVL variable",
        CONF_COMPONENTS: {uuid4().hex: component for component in device.components},
    }
    if with_area and device.area:
        data[CONF_AREA] = device.area
    return data
