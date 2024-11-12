"""Support for Automation Device Specification (ADS)."""

from collections import namedtuple
import ctypes
import logging
import struct
import threading

import pyads

_LOGGER = logging.getLogger(__name__)

# Tuple to hold data needed for notification
NotificationItem = namedtuple(  # noqa: PYI024
    "NotificationItem", "hnotify huser name plc_datatype callback"
)


class AdsHub:
    """Representation of an ADS connection."""

    def __init__(self, ads_client):
        """Initialize the ADS hub."""
        self._client = ads_client
        self._client.open()

        # All ADS devices are registered here
        self._devices = []
        self._notification_items = {}
        self._lock = threading.Lock()

    def shutdown(self, *args, **kwargs):
        """Shutdown ADS connection."""

        _LOGGER.debug("Shutting down ADS")
        for notification_item in self._notification_items.values():
            _LOGGER.debug(
                "Deleting device notification %d, %d",
                notification_item.hnotify,
                notification_item.huser,
            )
            try:
                self._client.del_device_notification(
                    notification_item.hnotify, notification_item.huser
                )
            except pyads.ADSError as err:
                _LOGGER.error(err)
        try:
            self._client.close()
        except pyads.ADSError as err:
            _LOGGER.error(err)

    def del_device_notification(self, device):
        """Delete a notification from the ADS devices based on device."""

        _LOGGER.debug("Attempting to delete notification for device: %s", device)
        with self._lock:
            # Look for the notification item by device name
            notification_item = None
            for hnotify, item in self._notification_items.items():
                _LOGGER.debug(
                    "Checking notification: %d for device %s", hnotify, item.name
                )
                if item.name == device:
                    notification_item = item
                    break

        if not notification_item:
            _LOGGER.error("Notification handle not found for device: %s", device)

            _LOGGER.debug("Current notifications: %s", self._notification_items)
            return

        try:
            # Remove the notification from the ADS client
            self._client.del_device_notification(
                notification_item.hnotify, notification_item.huser
            )
        except pyads.ADSError as err:
            _LOGGER.error("Error removing notification for %s: %s", device, err)
        else:
            _LOGGER.debug(
                "Removed device notification for variable %s, handle %d",
                device,
                notification_item.hnotify,
            )

        # After deletion, remove the item from the notification list
        with self._lock:
            self._notification_items.pop(notification_item.hnotify, None)

    def register_device(self, device):
        """Register a new device."""
        if device not in self._devices:
            self._devices.append(device)

    def unregister_device(self, device):
        """Unregister a device."""
        _LOGGER.debug("Unregistering device: %s", device)
        if device in self._devices:
            # Ensure that the notification is deleted when unregistering
            self.del_device_notification(device)
            self._devices.remove(device)
            _LOGGER.debug("Successfully unregistered device %s", device)
        else:
            _LOGGER.warning("Device %s not found in registered devices list", device)

    def write_by_name(self, name, value, plc_datatype):
        """Write a value to the device."""

        with self._lock:
            try:
                return self._client.write_by_name(name, value, plc_datatype)
            except pyads.ADSError as err:
                _LOGGER.error("Error writing %s: %s", name, err)

    def read_by_name(self, name, plc_datatype):
        """Read a value from the device."""

        with self._lock:
            try:
                return self._client.read_by_name(name, plc_datatype)
            except pyads.ADSError as err:
                _LOGGER.error("Error reading %s: %s", name, err)

    def add_device_notification(self, name, plc_datatype, callback):
        """Add a notification to the ADS devices."""

        attr = pyads.NotificationAttrib(ctypes.sizeof(plc_datatype))

        with self._lock:
            try:
                hnotify, huser = self._client.add_device_notification(
                    name, attr, self._device_notification_callback
                )
            except pyads.ADSError as err:
                _LOGGER.error("Error subscribing to %s: %s", name, err)
            else:
                hnotify = int(hnotify)
                self._notification_items[hnotify] = NotificationItem(
                    hnotify, huser, name, plc_datatype, callback
                )

                _LOGGER.debug(
                    "Added device notification %d for variable %s", hnotify, name
                )
                # Log the state of _notification_items after adding
            _LOGGER.debug("Current notifications: %s", self._notification_items)

    def _device_notification_callback(self, notification, name):
        """Handle device notifications."""
        contents = notification.contents
        hnotify = int(contents.hNotification)
        _LOGGER.debug("Received notification %d", hnotify)

        # Get dynamically sized data array
        data_size = contents.cbSampleSize
        data_address = (
            ctypes.addressof(contents)
            + pyads.structs.SAdsNotificationHeader.data.offset
        )
        data = (ctypes.c_ubyte * data_size).from_address(data_address)

        # Acquire notification item
        with self._lock:
            notification_item = self._notification_items.get(hnotify)

        if not notification_item:
            _LOGGER.error("Unknown device notification handle: %d", hnotify)
            return

        # Data parsing based on PLC data type
        plc_datatype = notification_item.plc_datatype
        unpack_formats = {
            pyads.PLCTYPE_BYTE: "<b",
            pyads.PLCTYPE_INT: "<h",
            pyads.PLCTYPE_UINT: "<H",
            pyads.PLCTYPE_SINT: "<b",
            pyads.PLCTYPE_USINT: "<B",
            pyads.PLCTYPE_DINT: "<i",
            pyads.PLCTYPE_UDINT: "<I",
            pyads.PLCTYPE_WORD: "<H",
            pyads.PLCTYPE_DWORD: "<I",
            pyads.PLCTYPE_LREAL: "<d",
            pyads.PLCTYPE_REAL: "<f",
            pyads.PLCTYPE_TOD: "<i",  # Treat as DINT
            pyads.PLCTYPE_DATE: "<i",  # Treat as DINT
            pyads.PLCTYPE_DT: "<i",  # Treat as DINT
            pyads.PLCTYPE_TIME: "<i",  # Treat as DINT
        }

        if plc_datatype == pyads.PLCTYPE_BOOL:
            value = bool(struct.unpack("<?", bytearray(data))[0])
        elif plc_datatype == pyads.PLCTYPE_STRING:
            value = (
                bytearray(data).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            )
        elif plc_datatype in unpack_formats:
            value = struct.unpack(unpack_formats[plc_datatype], bytearray(data))[0]
        else:
            value = bytearray(data)
            _LOGGER.warning("No callback available for this datatype")

        notification_item.callback(notification_item.name, value)

    def get_notification_handle(self, device):
        """Get the notification handle for a device, if available."""
        for hnotify, notification_item in self._notification_items.items():
            if notification_item.name == device:
                return hnotify
        return None
