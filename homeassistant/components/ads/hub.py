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

# Milliseconds. A device that has stopped answering will not answer the
# notification deletions either, and waiting the full timeout on each of a few
# hundred of them would take the teardown into the minutes.
ADS_TIMEOUT = 5000
TEARDOWN_TIMEOUT = 100

# The ADS server publishes its own state at this address, as a UINT16, and
# pushes it on change like any other notification. pyads does not name either
# constant.
ADSIGRP_DEVICE_DATA = 0xF100
ADSIOFFS_DEVDATA_ADSSTATE = 0x0000


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
        self._ads_state: int | None = None
        self._last_error: str | None = None
        self._state_handles: tuple[int, int] | None = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        """Return whether the ADS device is answering and running."""
        return self._connected

    @property
    def identifier(self) -> str:
        """Return a stable id for the device this hub is connected to."""
        return f"{self._client.ams_netid}:{self._client.ams_port}"

    @property
    def reachable(self) -> bool:
        """Return whether the ADS device is answering, running or not."""
        return self._ads_state is not None

    @property
    def ads_state(self) -> int | None:
        """Return the device's last known ADS state, None if it went quiet."""
        return self._ads_state

    @property
    def last_error(self) -> str | None:
        """Return why the device last stopped answering, if it did."""
        return self._last_error

    async def async_setup(self) -> None:
        """Connect to the ADS device and watch it from then on."""
        await self._hass.async_add_executor_job(self._connect)
        self._cancel_keepalive = async_track_time_interval(
            self._hass, self._async_check_connection, KEEPALIVE_INTERVAL
        )
        if not self._connected:
            # The device answers, but its PLC program is not running, so
            # there is nothing worth subscribing to yet.
            _LOGGER.warning(
                "The ADS device is reachable but not running (ADS state %s)",
                self._ads_state,
            )
            self._async_schedule_reconnect(RECONNECT_MIN_INTERVAL)

    def _connect(self) -> None:
        """Open the connection and find out what the device is doing."""
        self._client.open()
        self._client.set_timeout(ADS_TIMEOUT)
        # On Linux, open() only adds a route. Whether the device answers this
        # client's AMS NetID at all first shows up on a request.
        try:
            ads_state, _ = self._client.read_state()
        except pyads.ADSError:
            self._client.close()
            raise
        with self._lock:
            self._ads_state = ads_state
            self._last_error = None
            self._connected = ads_state == pyads.ADSSTATE_RUN
        self._subscribe_to_state()

    def _read_state(self) -> int | None:
        """Read what the device is doing, remembering it and any error."""
        with self._lock:
            if self._closed:
                return self._ads_state
            try:
                ads_state, _ = self._client.read_state()
            except pyads.ADSError as err:
                self._ads_state = None
                self._last_error = str(err)
            else:
                self._ads_state = ads_state
                self._last_error = None
            return self._ads_state

    def _subscribe_to_state(self) -> None:
        """Ask the device to push its state, so a change lands within a cycle.

        The keepalive still has to run: a device that stops answering
        altogether also stops sending notifications, and says nothing about it.
        A server that will not push its state leaves that probe as the only
        way to notice, which is what the integration did before.
        """
        attr = pyads.NotificationAttrib(
            ctypes.sizeof(pyads.PLCTYPE_UINT),
            max_delay=NOTIFICATION_MAX_DELAY,
            cycle_time=NOTIFICATION_CYCLE_TIME,
        )
        try:
            handles = self._client.add_device_notification(
                (ADSIGRP_DEVICE_DATA, ADSIOFFS_DEVDATA_ADSSTATE),
                attr,
                self._state_notification_callback,
            )
        except pyads.ADSError as err:
            _LOGGER.debug("The ADS device does not push its state: %s", err)
            return
        if handles is None:
            return
        # Deleting this one always raises: an address has no symbol handle, but
        # the ADS library releases one anyway. The notification still goes.
        self._state_handles = (int(handles[0]), int(handles[1]))

    def _state_notification_callback(
        self, notification: Any, address: tuple[int, int]
    ) -> None:
        """Handle the device reporting a change of its own ADS state."""
        contents = notification.contents
        data_address = (
            ctypes.addressof(contents)
            + pyads.structs.SAdsNotificationHeader.data.offset
        )
        data = (ctypes.c_ubyte * contents.cbSampleSize).from_address(data_address)
        ads_state = struct.unpack_from("<H", bytearray(data))[0]

        with self._lock:
            if self._closed or ads_state == self._ads_state:
                return
            self._ads_state = ads_state
            self._last_error = None

        _LOGGER.debug("The ADS device moved to state %s", ads_state)
        # Callbacks arrive on a pyads thread, so hop to the event loop.
        self._hass.loop.call_soon_threadsafe(self._async_state_changed, ads_state)

    @callback
    def _async_state_changed(self, ads_state: int) -> None:
        """Follow a pushed state change instead of waiting for the next probe."""
        if self._closed:
            return
        self._async_notify_listeners()
        if ads_state != pyads.ADSSTATE_RUN:
            if self._connected:
                self._hass.async_create_task(self._async_drop_and_retry())
            return
        if not self._connected:
            # It is running again, so there is no reason to sit out the backoff.
            self._async_retry_now()

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
            ads_state = await self._hass.async_add_executor_job(self._read_state)
        finally:
            self._probing = False
        if ads_state == pyads.ADSSTATE_RUN or self._closed:
            return
        if ads_state is None:
            _LOGGER.warning(
                "Lost the connection to the ADS device: %s", self._last_error
            )
        else:
            _LOGGER.warning("The ADS device stopped running (ADS state %s)", ads_state)
        await self._async_drop_and_retry()

    async def _async_drop_and_retry(self) -> None:
        """Give up the unusable connection and start rebuilding it."""
        if not self._connected:
            return
        await self._hass.async_add_executor_job(self._drop_connection)
        self._async_notify_listeners()
        self._async_schedule_reconnect(RECONNECT_MIN_INTERVAL)

    @callback
    def _async_retry_now(self) -> None:
        """Bring the pending reconnect attempt forward."""
        if self._cancel_reconnect is None:
            return
        self._cancel_reconnect()
        self._cancel_reconnect = None
        self._async_schedule_reconnect(0)

    def _drop_connection(self) -> None:
        """Give up the notifications, and the connection if the device is gone.

        A device that has stopped running has already invalidated every
        notification, so they go either way; the socket only has to be
        rebuilt when the device stopped answering altogether, which the ADS
        library does not recover from on its own.
        """
        with self._lock:
            self._connected = False
            self._subscription_ids_by_hnotify.clear()
            handles = [
                subscription.handles
                for subscription in self._subscriptions.values()
                if subscription.handles is not None
            ]
            for subscription in self._subscriptions.values():
                subscription.handles = None
            unreachable = self._ads_state is None
            if unreachable and self._state_handles is not None:
                # The socket is about to go, and the state notification with
                # it. A device that is merely stopped keeps pushing, which is
                # how the restart is noticed without waiting out the backoff.
                handles.append(self._state_handles)
                self._state_handles = None
        # Releasing waits for in-flight callbacks, which take the lock
        # themselves, so this has to run unlocked.
        self._release_notifications(handles)
        if not unreachable:
            return
        try:
            self._client.close()
        except pyads.ADSError as err:
            _LOGGER.debug("Closing the ADS connection failed: %s", err)

    def _release_notifications(self, handles: list[tuple[int, int]]) -> None:
        """Hand the notification handles back to the ADS library.

        This has to happen before the connection is closed. The library drops
        its dispatcher for a notification whether or not the device answers
        the deletion, and closing a connection whose dispatchers are still
        live takes the whole process down with it.
        """
        if self._ads_state is None:
            self._client.set_timeout(TEARDOWN_TIMEOUT)
        for hnotify, huser in handles:
            _LOGGER.debug("Deleting device notification %d, %d", hnotify, huser)
            try:
                self._client.del_device_notification(hnotify, huser)
            except pyads.ADSError as err:
                # Expected whenever the device is the reason we are here.
                _LOGGER.debug(
                    "Deleting device notification %d failed: %s", hnotify, err
                )

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
        previous_state = self._ads_state
        if await self._hass.async_add_executor_job(self._reconnect):
            _LOGGER.info("Reconnected to the ADS device")
            self._async_notify_listeners()
            return
        if self._ads_state != previous_state:
            # Still down, but differently so. The keepalive stops probing once
            # the connection is gone, so this is the only place a listener can
            # learn that the device went from silent to stopped, or back.
            self._async_notify_listeners()
        self._async_schedule_reconnect(min(delay * 2, RECONNECT_MAX_INTERVAL))

    def _reconnect(self) -> bool:
        """Reopen a closed connection, and resubscribe once the device runs."""
        try:
            self._client.open()
            self._client.set_timeout(ADS_TIMEOUT)
        except pyads.ADSError as err:
            _LOGGER.debug("Reopening the ADS connection failed: %s", err)
            return False
        ads_state = self._read_state()
        if ads_state is not None and self._state_handles is None:
            self._subscribe_to_state()
        if ads_state != pyads.ADSSTATE_RUN:
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
            self._connected = False
            handles = [
                subscription.handles
                for subscription in self._subscriptions.values()
                if subscription.handles is not None
            ]
            self._subscriptions.clear()
            self._subscription_ids_by_hnotify.clear()
            if self._state_handles is not None:
                handles.append(self._state_handles)
                self._state_handles = None
        # Deleting a notification waits for its in-flight callbacks, which take
        # the lock themselves, so this has to run unlocked.
        self._release_notifications(handles)
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
