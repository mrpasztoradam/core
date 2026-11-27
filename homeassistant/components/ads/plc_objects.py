"""Proof of concept: Home Assistant objects defined in the PLC program.

The PLC program defines objects by implementing interfaces Home Assistant
knows, such as I_HA_Light:

    PROPERTY IsOn : BOOL            {attribute 'monitoring' := 'call'}
    PROPERTY Brightness : USINT     {attribute 'monitoring' := 'call'}
    METHOD TurnOn : BOOL            {attribute 'TcRpcEnable'}
        VAR_INPUT nBrightness : USINT; END_VAR
    METHOD TurnOff : BOOL           {attribute 'TcRpcEnable'}

The data type table says which function blocks implement an interface
(TcImplements, followed through TcExtends), and the symbol table where their
instances are. An instance is named by {attribute 'ha.name' := '...'} on its
declaration and placed in the room of the function block it is declared in.
The state is followed by notifications on the properties, and the commands
call the methods by ADS RPC.
"""

from collections.abc import Callable
from ctypes import create_string_buffer
from dataclasses import dataclass
from functools import partial
import re
import struct
from typing import Any

import pyads
from pyads.constants import (
    ADSIGRP_SYM_DT_UPLOAD,
    ADSIGRP_SYM_UPLOADINFO2,
    ADSIOFFS_DEVDATA_ADSSTATE,
    PLCTYPE_STRING,
)

from homeassistant.const import Platform

# The interfaces Home Assistant knows, and the platform of their entities,
# most specific first: an RGBW light object is a light object too.
INTERFACES = {"I_HA_RgbwLight": Platform.LIGHT, "I_HA_Light": Platform.LIGHT}
NAME_ATTRIBUTE = "ha.name"
MAX_DEPTH = 10

_TYPEGUID, _COPYMASK, _METHODINFOS, _ATTRIBUTES = 0x80, 0x200, 0x800, 0x1000


@dataclass
class PlcObject:
    """An instance of a function block implementing an interface."""

    path: str
    fb_type: str
    interface: str
    name: str
    room: str | None = None
    floor: str | None = None


def _text(raw: bytes) -> str:
    return " ".join(raw.decode("utf-8", errors="replace").strip("\0").split())


def _parse(buf: bytes, ptr: int) -> tuple[dict[str, Any], int]:
    """Parse one data type entry, with its attributes."""
    entry_len, _, _, _, size, _, _, flags = struct.unpack_from("<8I", buf, ptr)
    name_len, type_len, comment_len, array_dims, sub_count = struct.unpack_from(
        "<5H", buf, ptr + 32
    )
    pos = ptr + 42
    name = _text(buf[pos : pos + name_len])
    pos += name_len + 1
    type_name = _text(buf[pos : pos + type_len])
    pos += type_len + 1 + comment_len + 1 + 8 * array_dims
    subs = []
    for _ in range(sub_count):
        sub, pos = _parse(buf, pos)
        subs.append(sub)
    attrs: dict[str, str] = {}
    if flags & _TYPEGUID:
        pos += 16
    if flags & _COPYMASK:
        pos += size
    if flags & _METHODINFOS:
        (count,) = struct.unpack_from("<H", buf, pos)
        pos += 2
        for _ in range(count):
            pos += struct.unpack_from("<I", buf, pos)[0]
    if flags & _ATTRIBUTES:
        (count,) = struct.unpack_from("<H", buf, pos)
        pos += 2
        for _ in range(count):
            attr_len, value_len = buf[pos], buf[pos + 1]
            pos += 2
            attr = _text(buf[pos : pos + attr_len])
            pos += attr_len + 1
            attrs[attr] = _text(buf[pos : pos + value_len])
            pos += value_len + 1
    return {"name": name, "type": type_name, "sub": subs, "attrs": attrs}, (
        ptr + entry_len
    )


def read_types(client: pyads.Connection) -> dict[str, dict[str, Any]]:
    """Read the PLC's data type table, with attributes."""
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
    return types


def _interface_of(types: dict[str, dict[str, Any]], type_name: str) -> str | None:
    """Return the most specific known interface a type implements.

    Interfaces are collected from the type and the types it extends; an
    interface's own EXTENDS is not needed, as the FB declares the derived one.
    """
    implemented: set[str] = set()
    seen = set()
    while type_name in types and type_name not in seen:
        seen.add(type_name)
        attrs = types[type_name]["attrs"]
        implemented.update(
            name.strip().rsplit(".", 1)[-1]
            for name in attrs.get("TcImplements", "").split(";")
        )
        type_name = attrs.get("TcExtends", "")
    return next((name for name in INTERFACES if name in implemented), None)


def humanize(name: str) -> str:
    """Turn HaObjCeilLed into "Ceil led"."""
    name = re.sub(r"^(HaObj|Ha|fb|FB_)", "", name)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", name).split()
    text = " ".join(words).lower()
    return text[:1].upper() + text[1:]


def find_objects(
    types: dict[str, dict[str, Any]], symbols: list[tuple[str, str]]
) -> list[PlcObject]:
    """Find the instances of the types implementing a known interface."""
    implementers = {
        name: interface
        for name in types
        if (interface := _interface_of(types, name)) is not None
    }
    found: list[PlcObject] = []

    def walk(path: str, type_name: str, attrs: dict[str, str], depth: int) -> None:
        if type_name in implementers:
            found.append(
                PlcObject(
                    path=path,
                    fb_type=type_name,
                    interface=implementers[type_name],
                    name=attrs.get(NAME_ATTRIBUTE) or humanize(path.rsplit(".", 1)[-1]),
                )
            )
            return
        entry = types.get(type_name)
        if entry is None or depth >= MAX_DEPTH:
            return
        for sub in entry["sub"]:
            if not sub["name"].startswith("_") and " OF " not in sub["type"]:
                walk(f"{path}.{sub['name']}", sub["type"], sub["attrs"], depth + 1)

    for name, type_name in symbols:
        if name.startswith("MAIN.") and name.count(".") == 1:
            walk(name, type_name, {}, 0)
    return found


def place(
    objects: list[PlcObject], read_description: Callable[[str], str | None]
) -> None:
    """Set each object's room and floor: the views it is declared in.

    The object's parent instance is its room and the room's parent its floor,
    named by their sDescription (TF8040 views), else by their own names.
    """
    descriptions: dict[str, str | None] = {}

    def described(path: str) -> str | None:
        if path.count(".") < 1:
            return None
        if path not in descriptions:
            descriptions[path] = read_description(f"{path}.sDescription")
        return descriptions[path] or humanize(path.rsplit(".", 1)[-1])

    for obj in objects:
        room = obj.path.rsplit(".", 1)[0]
        obj.room = described(room)
        if room.count(".") >= 2:
            obj.floor = described(room.rsplit(".", 1)[0])
