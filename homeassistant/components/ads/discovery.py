"""Proof of concept: discover a TF8040 project's structure and objects.

The PLC describes its project in two places:

- The data type table (ADSIGRP_SYM_DT_UPLOAD) and the symbol table give every
  instance of a base framework type (one with sObjectName, eObjectType,
  nInstanceID and nLevel), found by walking the members of the symbols.
- Each instance names its parent in the project structure
  (stAttributes.sParent) and, unless it is a plain structural view, a label
  saying what it is (stAttributes.sLabel, such as LblAgg_TemperatureRoomAir).

Structural views without a label are the house, the floors, the areas and the
rooms. Labelled views are aggregates, such as a temperature sensor or a light
output, and become devices; their objects become the entities. An object
directly in a structural view becomes a device of its own.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
import logging
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

from .objects import object_components

_LOGGER = logging.getLogger(__name__)

VIEW = 26
# Object types that become entities; loops, schedules, event classes and the
# like are left out.
ENTITY_OBJECT_TYPES = {10, 11, 12, 15, 16, 17, 20, 21, 22}
INSTANCE_MARK = {"sObjectName", "eObjectType", "nInstanceID", "nLevel"}
# Members that are framework plumbing, not part of the project.
SKIPPED_MEMBERS = {
    "Error",
    "Ready",
    "SplPLC",
    "SplBACnet",
    "fbEvents",
    "iParent",
    "iLabel",
    "iProfile",
    "aListEntry",
    "stRebuildTitle",
    "stProjStruct",
    "stAxisList",
    "fbActiveEvents",
    "aSplReference",
}
MAX_DEPTH = 10
READ_CHUNK = 100


@dataclass
class Node:
    """An instance of the project structure."""

    path: str
    fb_type: str
    members: frozenset[str]
    # Index group and offset of the instance, and each member's offset in it
    # and size, so its members can be sum read without a lookup per name.
    address: tuple[int, int] = (0, 0)
    layout: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)
    object_type: int | None = None
    description: str = ""
    name: str = ""
    parent: str | None = None
    label: str | None = None
    unit: int | None = None
    children: list[Node] = field(default_factory=list)
    parent_node: Node | None = field(default=None, repr=False)

    @property
    def is_structural(self) -> bool:
        """Return whether this is a house, floor, area or room view.

        Those views hold other views; a view that only holds objects is an
        aggregate, such as a temperature sensor or a light output. (The
        structural views are also the ones without a label, but reading every
        label one by one takes too long.)
        """
        return self.object_type == VIEW and any(
            child.object_type == VIEW for child in self.children
        )

    @property
    def is_entity_object(self) -> bool:
        """Return whether this object becomes an entity."""
        return self.object_type in ENTITY_OBJECT_TYPES


def _decode(raw: bytes) -> str:
    # Collapse runs of whitespace: descriptions are built from texts that may
    # end in a space, which would keep a child's from starting with its
    # parent's.
    return " ".join(raw.decode("utf-8", errors="replace").strip("\0").split())


def _parse_datatype(buf: bytes, ptr: int) -> tuple[dict[str, Any], int]:
    """Parse one entry of the data type table (AdsDatatypeEntry)."""
    entry_len, _, _, _, size, offset = struct.unpack_from("<6I", buf, ptr)
    name_len, type_len, comment_len, array_dims, sub_count = struct.unpack_from(
        "<HHHHH", buf, ptr + 32
    )
    pos = ptr + 42
    name = _decode(buf[pos : pos + name_len])
    pos += name_len + 1
    type_name = _decode(buf[pos : pos + type_len])
    pos += type_len + 1 + comment_len + 1 + 8 * array_dims
    subs = []
    for _ in range(sub_count):
        sub, pos = _parse_datatype(buf, pos)
        subs.append(sub)
    entry = {
        "name": name,
        "type": type_name,
        "size": size,
        "offset": offset,
        "sub": subs,
        "array": array_dims,
    }
    return entry, ptr + entry_len


def upload_datatypes(client: pyads.Connection) -> dict[str, dict[str, Any]]:
    """Read the PLC's data type table."""
    info = client.read(
        ADSIGRP_SYM_UPLOADINFO2,
        ADSIOFFS_DEVDATA_ADSSTATE,
        PLCTYPE_STRING,
        return_ctypes=True,
    )
    count, length = struct.unpack_from("<II", bytes(info), 8)
    from ctypes import create_string_buffer  # noqa: PLC0415

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
        entry, ptr = _parse_datatype(buf, ptr)
        types[entry["name"]] = entry
    return types


def find_instances(
    types: dict[str, dict[str, Any]],
    symbols: Iterable[tuple[str, str, int, int]],
) -> list[Node]:
    """Walk the program's symbols for instances of the base framework."""
    instance_types = {
        name
        for name, entry in types.items()
        if {sub["name"] for sub in entry["sub"]} >= INSTANCE_MARK
    }
    found: list[Node] = []

    def walk(path: str, type_name: str, depth: int, group: int, offset: int) -> None:
        entry = types.get(type_name)
        if entry is None:
            return
        if type_name in instance_types:
            found.append(
                Node(
                    path,
                    type_name,
                    frozenset(sub["name"] for sub in entry["sub"]),
                    address=(group, offset),
                    layout={
                        sub["name"]: (sub["offset"], sub["size"])
                        for sub in entry["sub"]
                    },
                )
            )
        if depth >= MAX_DEPTH:
            return
        for sub in entry["sub"]:
            # Arrays of objects are left out of this proof of concept.
            if (
                sub["name"].startswith("_")
                or sub["name"] in SKIPPED_MEMBERS
                or " OF " in sub["type"]
            ):
                continue
            walk(
                f"{path}.{sub['name']}",
                sub["type"],
                depth + 1,
                group,
                offset + sub["offset"],
            )

    for name, type_name, group, offset in symbols:
        if name.startswith("MAIN.") and name.count(".") == 1:
            walk(name, type_name, 0, group, offset)
    return found


def describe(
    nodes: list[Node],
    read_raw: Callable[[list[tuple[int, int, int]]], bytes],
    read_reference: Callable[[str], str | None],
) -> None:
    """Read what each instance is and where it sits in the project.

    The plain members are sum read at their addresses from the data type
    table, so no symbol has to be looked up by name. The parent and the label
    are references, which a sum read cannot follow, so the few needed are read
    one by one.
    """
    members = ("eObjectType", "sDescription", "sObjectName", "eUnit")
    requests: list[tuple[Node, str, tuple[int, int, int]]] = []
    for node in nodes:
        group, base = node.address
        for member in members:
            if member in node.layout:
                offset, size = node.layout[member]
                requests.append((node, member, (group, base + offset, size)))
    for start in range(0, len(requests), READ_CHUNK):
        chunk = requests[start : start + READ_CHUNK]
        response = bytes(read_raw([request for _, _, request in chunk]))
        pos = 4 * len(chunk)
        for index, (node, member, (_, _, size)) in enumerate(chunk):
            error = struct.unpack_from("<I", response, 4 * index)[0]
            raw = response[pos : pos + size]
            pos += size
            if error:
                continue
            if member == "eObjectType":
                node.object_type = raw[0]
            elif member == "eUnit":
                node.unit = int.from_bytes(raw, "little")
            elif member == "sDescription":
                node.description = _decode(raw.split(b"\0", 1)[0])
            else:
                node.name = _decode(raw.split(b"\0", 1)[0])
    for node in nodes:
        if node.object_type not in (10, 11, 12):
            node.unit = None
        # A nested instance's parent is the instance it is nested in. The
        # program's top-level blocks name theirs, which places the floors
        # and trades under the house.
        if node.path.count(".") == 1:
            node.parent = read_reference(f"{node.path}.stAttributes.sParent")
        # The label says what a binary object detects; structural views have
        # none, and reading it fails (1796).
        if node.object_type in (15, 16, 17):
            node.label = read_reference(f"{node.path}.stAttributes.sLabel") or None


def link(nodes: list[Node]) -> list[Node]:
    """Link the instances into the project tree and return its roots.

    The parent is given as the full symbol path, prefixed by the PLC project's
    name (Haz.MAIN.FSZ), which the instance paths do not have.
    """
    by_path = {node.path: node for node in nodes}
    roots = []
    for node in nodes:
        parent = None
        if node.parent and "." in node.parent:
            parent = by_path.get(node.parent.split(".", 1)[1])
        else:
            prefix = node.path
            while parent is None and prefix.count(".") > 1:
                prefix = prefix.rsplit(".", 1)[0]
                parent = by_path.get(prefix)
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
            node.parent_node = parent
    return roots


# What a label says an object or aggregate measures, by a word in the label.
LABEL_DEVICE_CLASSES = {
    "binary_sensor": [
        ("Presence", "occupancy"),
        ("Motion", "motion"),
        ("Window", "window"),
        ("Door", "door"),
        ("Fault", "problem"),
        ("Alarm", "problem"),
    ],
    "sensor": [
        ("Temperature", "temperature"),
        ("Humidity", "humidity"),
    ],
}


@dataclass
class DiscoveredDevice:
    """A device to add: an aggregate, or an object on its own."""

    key: str
    title: str
    model: str
    area: str | None
    objects: list[Node]


@dataclass
class Plan:
    """What discovery found, laid out the way Home Assistant shows it."""

    house: str
    # Floor name to the names of its areas.
    floors: dict[str, list[str]] = field(default_factory=dict)
    # Areas that are on no floor, such as the building's trades.
    other_areas: list[str] = field(default_factory=list)
    # Area name to the intermediate views it sits in, kept as labels.
    area_labels: dict[str, set[str]] = field(default_factory=dict)
    devices: list[DiscoveredDevice] = field(default_factory=list)

    @property
    def entity_count(self) -> int:
        """Return how many objects become entities."""
        return sum(len(device.objects) for device in self.devices)


def short_name(node: Node, parent: Node | None) -> str:
    """Return the description without the parent's, which it often repeats.

    Building objects are described by their whole path, such as "House -
    Main building - Heating - Heating circuit - Flow temperature".
    """
    text = node.description or node.name or node.path.rsplit(".", 1)[-1]
    while parent is not None:
        if parent.description and text.startswith(f"{parent.description} - "):
            return text[len(parent.description) + 3 :]
        parent = parent.parent_node
    return text


def default_floors(root: Node) -> list[Node]:
    """Return the root's views that look like floors rather than a building.

    Floors are described on their own ("Upstairs"), while the building's view
    repeats the house's description ("House - Main building").
    """
    return [
        child
        for child in root.children
        if child.is_structural
        and not (
            root.description and child.description.startswith(f"{root.description} - ")
        )
    ]


def build_plan(root: Node, floors: list[Node]) -> Plan:
    """Lay the project tree out as floors, areas and devices."""
    plan = Plan(house=root.description or root.name)
    floor_ids = {id(floor) for floor in floors}
    devices: dict[str, DiscoveredDevice] = {}

    def area_for(node: Node) -> tuple[str | None, str | None, set[str]]:
        """Return the area, floor and labels of what sits in this node."""
        chain = []
        parent = node.parent_node
        while parent is not None:
            chain.append(parent)
            parent = parent.parent_node
        # chain runs from the node's parent up to the root.
        structural = [n for n in chain if n.is_structural]
        floor = next((n for n in structural if id(n) in floor_ids), None)
        if floor is not None:
            inner = structural[: structural.index(floor)]
            if not inner:
                return floor.description, floor.description, set()
            room = inner[0]
            labels = {short_name(n, n.parent_node) for n in inner[1:]}
            return short_name(room, room.parent_node), floor.description, labels
        # Outside the floors: the trade the node belongs to, below the root
        # and its building view.
        trades = [n for n in structural if n is not root and n.parent_node is not root]
        if trades:
            trade = trades[-1]
            return short_name(trade, trade.parent_node), None, set()
        return None, None, set()

    def visit(node: Node) -> None:
        for child in node.children:
            visit(child)
        if not node.is_entity_object:
            return
        parent = node.parent_node
        owner = parent if parent is not None and not parent.is_structural else node
        device = devices.get(owner.path)
        if device is None:
            area, floor, labels = area_for(owner)
            device = DiscoveredDevice(
                key=owner.path,
                title=short_name(owner, owner.parent_node),
                model=owner.fb_type.rsplit(".", 1)[-1],
                area=area,
                objects=[],
            )
            devices[owner.path] = device
            if area is not None:
                if floor is None:
                    if area not in plan.other_areas:
                        plan.other_areas.append(area)
                else:
                    rooms = plan.floors.setdefault(floor, [])
                    if area not in rooms:
                        rooms.append(area)
                if labels:
                    plan.area_labels.setdefault(area, set()).update(labels)
        device.objects.append(node)

    for floor in floors:
        plan.floors.setdefault(floor.description, [])
    visit(root)
    plan.devices = list(devices.values())
    return plan


def device_class_for(platform: str, node: Node, owner_label: str | None) -> str | None:
    """Return the device class a label points to, if any."""
    for label in (node.label, owner_label):
        for word, device_class in LABEL_DEVICE_CLASSES.get(platform, []):
            if label and word in label:
                return device_class
    return None


def plan_subentries(plan: Plan) -> list[tuple[str, str, dict[str, Any]]]:
    """Return the unique ID, title and data of a device subentry per device."""
    subentries = []
    for device in plan.devices:
        owner_label = None
        single = len(device.objects) == 1
        components: list[dict[str, Any]] = []
        for node in device.objects:
            platform = "binary_sensor" if node.object_type in (15, 16, 17) else "sensor"
            components.extend(
                object_components(
                    node.path,
                    node.object_type or 0,
                    node.unit,
                    node.members,
                    # A device of one object is named after it already.
                    name=None if single else short_name(node, node.parent_node),
                    device_class=device_class_for(platform, node, owner_label),
                )
            )
        data: dict[str, Any] = {
            "object": device.key,
            "model": device.model.removeprefix("FB_BA_"),
            "components": {uuid4().hex: component for component in components},
        }
        if device.area:
            data["area"] = device.area
        subentries.append((device.key, device.title, data))
    return subentries
