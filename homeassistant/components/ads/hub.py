"""Support for Automation Device Specification (ADS)."""

from collections.abc import Callable
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from itertools import count
import logging
import struct
import threading
from typing import Any

import pyads

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

_LOGGER = logging.getLogger(__name__)

# Both are milliseconds. pyads defaults to 1e-4 ms (100 ns) for each, which asks
# the ADS server for the tightest possible change detection with no batching.
# The cycle time tracks the PLC task cycle TF8040 recommends, since checking
# faster than the program can change a value is wasted load; the delay lets the
# router batch several changes into one telegram.
NOTIFICATION_CYCLE_TIME = 45.0
NOTIFICATION_MAX_DELAY = 100.0

# The ADS device never announces that it went away, so the connection has to be
# probed. A drop is noticed at most one interval after it happened.
KEEPALIVE_INTERVAL = timedelta(seconds=30)

# A PLC being restarted or redeployed is gone for minutes, so back off instead
# of asking the router the same question every few seconds.
RECONNECT_MIN_INTERVAL = 5.0
RECONNECT_MAX_INTERVAL = 300.0


@dataclass
class AdsSubscription:
    """A device notification, and the handles the current connection holds it by.

    The handles are reissued by the ADS device, so they do not survive a
    reconnect; everything needed to ask for them again does.
    """

    name: str
    plc_datatype: type
    callback: Callable[[str, Any], None]
    handles: tuple[int, int] | None = None


# Types not listed here are handled separately or unsupported.
UNPACK_FORMATS = {
    pyads.PLCTYPE_BYTE: "<B",
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
    pyads.PLCTYPE_TOD: "<i",  # c_int, unlike the other time types
    pyads.PLCTYPE_DATE: "<I",
    pyads.PLCTYPE_DT: "<I",
    pyads.PLCTYPE_TIME: "<I",
}


class AdsHub:
    """Representation of an ADS connection."""

    def __init__(self, hass: HomeAssistant, ads_client: pyads.Connection) -> None:
        """Initialize the ADS hub."""
        self._hass = hass
        self._client = ads_client

        self._subscriptions: dict[int, AdsSubscription] = {}
        self._next_subscription_id = count(1)
        self._subscription_ids_by_hnotify: dict[int, int] = {}
        self._connection_listeners: list[CALLBACK_TYPE] = []
        self._cancel_keepalive: CALLBACK_TYPE | None = None
        self._cancel_reconnect: CALLBACK_TYPE | None = None
        self._probing = False
        self._connected = False
        self._closed = False
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        """Return whether the ADS device is currently answering."""
        return self._connected

    async def async_setup(self) -> None:
        """Connect to the ADS device and watch the connection from then on."""
        await self._hass.async_add_executor_job(self._connect)
        self._cancel_keepalive = async_track_time_interval(
            self._hass, self._async_check_connection, KEEPALIVE_INTERVAL
        )

    def _connect(self) -> None:
        """Open the connection and confirm the device answers on it."""
        self._client.open()
        # On Linux, open() only adds a route. Whether the device answers this
        # client's AMS NetID at all first shows up on a request.
        try:
            self._client.read_state()
        except pyads.ADSError:
            self._client.close()
            raise
        with self._lock:
            self._connected = True

    @callback
    def async_add_connection_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Register a listener called whenever the connection comes or goes."""
        self._connection_listeners.append(listener)
        return partial(self._connection_listeners.remove, listener)

    @callback
    def _async_notify_listeners(self) -> None:
        """Tell the entities that the connection state changed."""
        for listener in list(self._connection_listeners):
            listener()

    async def _async_check_connection(self, now: datetime) -> None:
        """Probe the connection, and start rebuilding it once it has dropped."""
        if not self._connected or self._probing:
            return
        self._probing = True
        try:
            error = await self._hass.async_add_executor_job(self._probe)
        finally:
            self._probing = False
        if error is None:
            return
        _LOGGER.warning("Lost the connection to the ADS device: %s", error)
        await self._hass.async_add_executor_job(self._drop_connection)
        self._async_notify_listeners()
        self._async_schedule_reconnect(RECONNECT_MIN_INTERVAL)

    def _probe(self) -> str | None:
        """Return why the ADS device is not answering, or None if it is."""
        with self._lock:
            if self._closed:
                return None
            try:
                self._client.read_state()
            except pyads.ADSError as err:
                return str(err)
            return None

    def _drop_connection(self) -> None:
        """Forget the notification handles the broken connection held.

        The connection itself is deliberately left open. Closing it tears
        down the notification dispatchers inside the ADS library while it is
        still delivering on them, which takes the whole process with it, and
        the library owns the socket either way.
        """
        with self._lock:
            self._connected = False
            self._subscription_ids_by_hnotify.clear()
            for subscription in self._subscriptions.values():
                subscription.handles = None

    @callback
    def _async_schedule_reconnect(self, delay: float) -> None:
        """Try again after a delay that grows while the device stays away."""
        if self._closed:
            return
        self._cancel_reconnect = async_call_later(
            self._hass, delay, partial(self._async_reconnect, delay)
        )

    async def _async_reconnect(self, delay: float, now: datetime) -> None:
        """Put the subscriptions back once the device answers again."""
        self._cancel_reconnect = None
        if self._closed:
            return
        if await self._hass.async_add_executor_job(self._reconnect):
            _LOGGER.info("Reconnected to the ADS device")
            self._async_notify_listeners()
            return
        self._async_schedule_reconnect(min(delay * 2, RECONNECT_MAX_INTERVAL))

    def _reconnect(self) -> bool:
        """Subscribe again once the device answers, on the same connection.

        Nothing is reopened: the port was never closed, so what has to come
        back is the notifications, which the device forgets when it restarts.
        """
        if (error := self._probe()) is not None:
            _LOGGER.debug("The ADS device is still not answering: %s", error)
            return False
        with self._lock:
            self._connected = True
            subscriptions = list(self._subscriptions.items())
        for subscription_id, subscription in subscriptions:
            self._register(subscription_id, subscription)
        return True

    def shutdown(self) -> None:
        """Shutdown ADS connection."""

        _LOGGER.debug("Shutting down ADS")
        with self._lock:
            self._closed = True
            connected = self._connected
            self._connected = False
            subscriptions = list(self._subscriptions.values())
            self._subscriptions.clear()
            self._subscription_ids_by_hnotify.clear()
        # Deleting a notification waits for its in-flight callbacks, which take
        # the lock themselves, so this has to run unlocked.
        for subscription in subscriptions:
            if (handles := subscription.handles) is None:
                continue
            _LOGGER.debug("Deleting device notification %d, %d", *handles)
            try:
                self._client.del_device_notification(*handles)
            except pyads.ADSError as err:
                _LOGGER.error(err)
        if not connected:
            # Closing a connection the device has already dropped is what
            # crashes the ADS library; the port goes with the process anyway.
            return
        try:
            self._client.close()
        except pyads.ADSError as err:
            _LOGGER.error(err)

    async def async_shutdown(self, event: Event | None = None) -> None:
        """Stop watching the connection, then tear it down."""
        if self._cancel_keepalive is not None:
            self._cancel_keepalive()
            self._cancel_keepalive = None
        if self._cancel_reconnect is not None:
            self._cancel_reconnect()
            self._cancel_reconnect = None
        await self._hass.async_add_executor_job(self.shutdown)

    def write_by_name(self, name: str, value: Any, plc_datatype: type) -> None:
        """Write a value to the device."""

        with self._lock:
            # The lock is released again before the client is closed, so I/O
            # started after shutdown began would race the teardown.
            if self._closed:
                _LOGGER.debug("Not writing %s, the hub is shut down", name)
                return
            try:
                self._client.write_by_name(name, value, plc_datatype)
            except pyads.ADSError as err:
                _LOGGER.error("Error writing %s: %s", name, err)

    def read_by_name(self, name: str, plc_datatype: type) -> Any:
        """Read a value from the device."""

        with self._lock:
            if self._closed:
                _LOGGER.debug("Not reading %s, the hub is shut down", name)
                return None
            try:
                return self._client.read_by_name(name, plc_datatype)
            except pyads.ADSError as err:
                _LOGGER.error("Error reading %s: %s", name, err)
                return None

    def subscribe(
        self,
        name: str,
        plc_datatype: type,
        notification_callback: Callable[[str, Any], None],
    ) -> int | None:
        """Subscribe to a variable, returning an id to unsubscribe it by.

        The id identifies the subscription for as long as the hub lives, unlike
        the notification handle, which a reconnect replaces.
        """

        with self._lock:
            if self._closed:
                _LOGGER.debug("Not subscribing to %s, the hub is shut down", name)
                return None
            subscription_id = next(self._next_subscription_id)
            subscription = AdsSubscription(name, plc_datatype, notification_callback)
            self._subscriptions[subscription_id] = subscription

        self._register(subscription_id, subscription)
        return subscription_id

    def _register(self, subscription_id: int, subscription: AdsSubscription) -> None:
        """Ask the ADS device to notify on a subscription's variable.

        A subscription that cannot be registered stays on the hub, so a
        variable the device does not have yet is picked up by a later reconnect.
        """

        attr = pyads.NotificationAttrib(
            ctypes.sizeof(subscription.plc_datatype),
            max_delay=NOTIFICATION_MAX_DELAY,
            cycle_time=NOTIFICATION_CYCLE_TIME,
        )

        with self._lock:
            if not self._connected or subscription_id not in self._subscriptions:
                return
            try:
                handles = self._client.add_device_notification(
                    subscription.name, attr, self._device_notification_callback
                )
            except pyads.ADSError as err:
                _LOGGER.error("Error subscribing to %s: %s", subscription.name, err)
                return
            if handles is None:
                # pyads returns None instead of raising once the port is closed.
                _LOGGER.debug(
                    "Not subscribing to %s, the connection is closed", subscription.name
                )
                return
            hnotify, huser = int(handles[0]), int(handles[1])
            subscription.handles = (hnotify, huser)
            self._subscription_ids_by_hnotify[hnotify] = subscription_id

            _LOGGER.debug(
                "Added device notification %d for variable %s",
                hnotify,
                subscription.name,
            )

    def unsubscribe(self, subscription_id: int) -> None:
        """Drop a single subscription."""

        with self._lock:
            subscription = self._subscriptions.pop(subscription_id, None)
            handles = None if subscription is None else subscription.handles
            if handles is not None:
                del self._subscription_ids_by_hnotify[handles[0]]
        if handles is None:
            # Already gone, most likely torn down by shutdown(), or never
            # registered on the current connection.
            return
        _LOGGER.debug("Deleting device notification %d", handles[0])
        # Deleting waits for in-flight callbacks, which take the lock
        # themselves, so this has to run unlocked.
        try:
            self._client.del_device_notification(*handles)
        except pyads.ADSError as err:
            _LOGGER.error(err)

    def _device_notification_callback(self, notification: Any, name: str) -> None:
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

        # Acquire the subscription the handle belongs to
        with self._lock:
            subscription_id = self._subscription_ids_by_hnotify.get(hnotify)
            subscription = (
                None
                if subscription_id is None
                else self._subscriptions.get(subscription_id)
            )

        if subscription is None:
            _LOGGER.error("Unknown device notification handle: %d", hnotify)
            return

        value: Any
        plc_datatype = subscription.plc_datatype
        if plc_datatype == pyads.PLCTYPE_BOOL:
            value = bool(struct.unpack("<?", bytearray(data))[0])
        elif plc_datatype == pyads.PLCTYPE_STRING:
            value = (
                bytearray(data).split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
            )
        elif plc_datatype in UNPACK_FORMATS:
            value = struct.unpack(UNPACK_FORMATS[plc_datatype], bytearray(data))[0]
        else:
            value = bytearray(data)
            _LOGGER.warning("No callback available for this datatype")

        subscription.callback(subscription.name, value)
