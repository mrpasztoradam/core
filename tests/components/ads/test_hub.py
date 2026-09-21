"""Test the ADS hub."""

from collections.abc import AsyncGenerator
import ctypes
from datetime import timedelta
import struct
import threading
from typing import Any
from unittest.mock import MagicMock, call

import pyads
import pytest

from homeassistant.components.ads.hub import (
    KEEPALIVE_INTERVAL,
    NOTIFICATION_CYCLE_TIME,
    NOTIFICATION_MAX_DELAY,
    RECONNECT_MIN_INTERVAL,
    TEARDOWN_TIMEOUT,
    AdsHub,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import build_notification
from .conftest import DEVICE_STATE_ADDRESS
from .const import AMS_NET_ID, PORT, STATE_HANDLES

from tests.common import async_fire_time_changed


@pytest.fixture
def ads_client() -> MagicMock:
    """Return a mocked pyads client for a device that is up and running."""
    client = MagicMock()
    client.ams_netid = AMS_NET_ID
    client.ams_port = PORT
    client.read_state.return_value = (pyads.ADSSTATE_RUN, pyads.ADSSTATE_RUN)

    def add_device_notification(
        data: str | tuple[int, int], attr: pyads.NotificationAttrib, callback: Any
    ) -> tuple[int, int] | None:
        """Issue a handle of its own for the notification on the device state."""
        if data == DEVICE_STATE_ADDRESS:
            client.state_callback = callback
            return STATE_HANDLES
        return client.add_device_notification.return_value

    client.add_device_notification.side_effect = add_device_notification
    return client


@pytest.fixture
async def hub(hass: HomeAssistant, ads_client: MagicMock) -> AsyncGenerator[AdsHub]:
    """Return an AdsHub connected to the mocked client."""
    hub = AdsHub(hass, ads_client)
    await hub.async_setup()
    yield hub
    await hub.async_shutdown()


async def drop_connection(hass: HomeAssistant, ads_client: MagicMock) -> None:
    """Let the next keepalive probe find the device gone."""
    ads_client.read_state.side_effect = pyads.ADSError(text="timeout")
    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)


async def push_state(
    hass: HomeAssistant, ads_client: MagicMock, ads_state: int
) -> None:
    """Deliver an ADS state the way the device pushes one, on its own thread."""
    ads_client.read_state.side_effect = None
    ads_client.read_state.return_value = (ads_state, pyads.ADSSTATE_RUN)
    thread = threading.Thread(
        target=ads_client.state_callback,
        args=(
            build_notification(STATE_HANDLES[0], struct.pack("<H", ads_state)),
            DEVICE_STATE_ADDRESS,
        ),
    )
    thread.start()
    thread.join()
    await hass.async_block_till_done(wait_background_tasks=True)


async def restore_connection(
    hass: HomeAssistant, ads_client: MagicMock, attempt: int = 1
) -> None:
    """Let the device answer again, and run the pending reconnect attempt.

    Each failed attempt doubles the wait for the next, so `attempt` says how
    far ahead to fire.
    """
    ads_client.read_state.side_effect = None
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL * 2**attempt + 1),
    )
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_setup_confirms_the_device_answers(
    hass: HomeAssistant, ads_client: MagicMock
) -> None:
    """Test setting up opens the connection and makes a request on it."""
    hub = AdsHub(hass, ads_client)

    await hub.async_setup()

    ads_client.open.assert_called_once()
    ads_client.read_state.assert_called_once()
    assert hub.connected
    assert hub.ads_state == pyads.ADSSTATE_RUN
    assert hub.last_error is None

    await hub.async_shutdown()


async def test_setup_of_a_device_that_is_not_running(
    hass: HomeAssistant, ads_client: MagicMock
) -> None:
    """Test a reachable device whose PLC program is stopped.

    Subscribing to its symbols would be pointless, so the hub comes up
    disconnected and waits for the device to start running.
    """
    ads_client.read_state.return_value = (pyads.ADSSTATE_CONFIG, pyads.ADSSTATE_RUN)
    hub = AdsHub(hass, ads_client)

    await hub.async_setup()

    assert not hub.connected
    assert hub.ads_state == pyads.ADSSTATE_CONFIG

    ads_client.read_state.return_value = (pyads.ADSSTATE_RUN, pyads.ADSSTATE_RUN)
    await restore_connection(hass, ads_client)

    assert hub.connected

    await hub.async_shutdown()


async def test_setup_closes_a_connection_that_stays_silent(
    hass: HomeAssistant, ads_client: MagicMock
) -> None:
    """Test a port the device never answers on is not left open.

    On Linux, open() only adds a route, so a device that has no route back to
    this client refuses the first request rather than the connection.
    """
    ads_client.read_state.side_effect = pyads.ADSError(text="timeout")
    hub = AdsHub(hass, ads_client)

    with pytest.raises(pyads.ADSError):
        await hub.async_setup()

    ads_client.close.assert_called_once()
    assert not hub.connected
    assert hub.ads_state is None


async def test_write_by_name(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test writing a value by name."""
    hub.write_by_name("GVL.test", 42, pyads.PLCTYPE_INT)

    ads_client.write_by_name.assert_called_once_with("GVL.test", 42, pyads.PLCTYPE_INT)


async def test_read_by_name(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test reading a value by name."""
    ads_client.read_by_name.return_value = 42

    assert hub.read_by_name("GVL.test", pyads.PLCTYPE_INT) == 42


@pytest.mark.parametrize(
    ("method", "args"),
    [
        pytest.param("write_by_name", ("GVL.test", 42, pyads.PLCTYPE_INT), id="write"),
        pytest.param("read_by_name", ("GVL.test", pyads.PLCTYPE_INT), id="read"),
    ],
)
async def test_io_error_is_logged(
    hub: AdsHub, ads_client: MagicMock, method: str, args: tuple
) -> None:
    """Test an I/O error is logged instead of raised."""
    getattr(ads_client, method).side_effect = pyads.ADSError(text="timeout")

    assert getattr(hub, method)(*args) is None


@pytest.mark.parametrize(
    ("method", "args"),
    [
        pytest.param("write_by_name", ("GVL.test", 42, pyads.PLCTYPE_INT), id="write"),
        pytest.param("read_by_name", ("GVL.test", pyads.PLCTYPE_INT), id="read"),
    ],
)
async def test_io_after_shutdown(
    hub: AdsHub, ads_client: MagicMock, method: str, args: tuple
) -> None:
    """Test I/O is refused once the hub is shut down."""
    await hub.async_shutdown()

    assert getattr(hub, method)(*args) is None

    getattr(ads_client, method).assert_not_called()


async def test_subscribe(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test subscribing registers a notification and returns its id."""
    ads_client.add_device_notification.return_value = (1, 2)

    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    assert subscription is not None
    assert ads_client.add_device_notification.call_args.args[0] == "GVL.test"


async def test_subscribe_error_is_logged(
    hub: AdsHub, ads_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a notification subscription error is logged instead of raised."""
    ads_client.add_device_notification.side_effect = pyads.ADSError(text="timeout")

    assert hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock()) is not None

    assert "Error subscribing to GVL.test" in caplog.text


async def test_subscribe_closed_connection(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test subscribing on a closed connection leaves no handle behind.

    pyads returns None instead of raising once the port is gone.
    """
    ads_client.add_device_notification.return_value = None

    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    assert subscription is not None
    hub.unsubscribe(subscription)
    ads_client.del_device_notification.assert_not_called()


async def test_subscribe_after_shutdown(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test a late subscription is refused once the hub is shut down."""
    await hub.async_shutdown()
    ads_client.add_device_notification.reset_mock()

    assert hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock()) is None

    ads_client.add_device_notification.assert_not_called()


async def test_unsubscribe(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test dropping a subscription deletes its notification."""
    ads_client.add_device_notification.return_value = (1, 2)
    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    hub.unsubscribe(subscription)

    ads_client.del_device_notification.assert_called_once_with(1, 2)


async def test_unsubscribe_unknown_id(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test dropping a subscription that shutdown already tore down."""
    hub.unsubscribe(1)

    ads_client.del_device_notification.assert_not_called()


async def test_unsubscribe_error_is_logged(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test a deletion error is logged instead of raised."""
    ads_client.add_device_notification.return_value = (1, 2)
    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())
    ads_client.del_device_notification.side_effect = pyads.ADSError(text="timeout")

    hub.unsubscribe(subscription)

    ads_client.del_device_notification.assert_called_once_with(1, 2)


async def test_shutdown(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test shutdown deletes notifications and closes the connection."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    await hub.async_shutdown()

    assert ads_client.del_device_notification.call_args_list == [
        call(1, 2),
        call(*STATE_HANDLES),
    ]
    ads_client.close.assert_called_once()


async def test_shutdown_ignores_ads_errors(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test shutdown still closes the connection if cleanup calls fail."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())
    ads_client.del_device_notification.side_effect = pyads.ADSError(text="timeout")
    ads_client.close.side_effect = pyads.ADSError(text="timeout")

    await hub.async_shutdown()

    ads_client.close.assert_called_once()


async def test_shutdown_stops_probing(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the connection is left alone once the hub is shut down."""
    await hub.async_shutdown()
    ads_client.read_state.reset_mock()

    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    ads_client.read_state.assert_not_called()


async def test_a_healthy_connection_is_left_alone(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that keeps answering is not reconnected to."""
    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hub.connected
    # The probe is the one request on top of the one setup made.
    assert ads_client.read_state.call_count == 2
    ads_client.close.assert_not_called()


async def test_connection_loss_is_announced(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that stops answering is noticed and reported."""
    listener = MagicMock()
    hub.async_add_connection_listener(listener)

    await drop_connection(hass, ads_client)

    assert not hub.connected
    assert hub.ads_state is None
    assert hub.last_error is not None
    listener.assert_called_once()


async def test_a_drop_releases_the_notifications_before_closing(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the ADS library is given its dispatchers back before the close.

    Closing a connection whose notification dispatchers are still live
    crashes the library and takes the whole process down with it.
    """
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())

    await drop_connection(hass, ads_client)

    assert ads_client.method_calls.index(
        call.del_device_notification(1, 2)
    ) < ads_client.method_calls.index(call.close())


async def test_a_drop_does_not_wait_on_a_device_that_is_gone(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the teardown does not spend the full ADS timeout per notification."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())

    await drop_connection(hass, ads_client)

    ads_client.set_timeout.assert_called_with(TEARDOWN_TIMEOUT)


async def test_connection_listener_can_be_dropped(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a removed entity is not called back on."""
    listener = MagicMock()
    hub.async_add_connection_listener(listener)()

    await drop_connection(hass, ads_client)

    listener.assert_not_called()


async def test_a_device_that_stops_running_is_not_disconnected_from(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a stopped PLC program takes the entities down but not the socket.

    The device is still answering, so there is nothing wrong with the
    connection; only the notifications are worthless.
    """
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())
    ads_client.read_state.return_value = (pyads.ADSSTATE_STOP, pyads.ADSSTATE_RUN)

    async_fire_time_changed(hass, dt_util.utcnow() + KEEPALIVE_INTERVAL)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not hub.connected
    assert hub.ads_state == pyads.ADSSTATE_STOP
    ads_client.close.assert_not_called()
    ads_client.del_device_notification.assert_called_once_with(1, 2)

    # Still stopped, so there is still nothing to subscribe to.
    await restore_connection(hass, ads_client)
    assert not hub.connected

    ads_client.read_state.return_value = (pyads.ADSSTATE_RUN, pyads.ADSSTATE_RUN)
    await restore_connection(hass, ads_client, attempt=2)

    assert hub.connected


async def test_identifier_names_the_device(hub: AdsHub) -> None:
    """Test the hub is identified by the device it is connected to."""
    assert hub.identifier == f"{AMS_NET_ID}:{PORT}"


async def test_a_failed_reconnect_announces_a_changed_state(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that answers again without running is still reported.

    The keepalive stops probing once the connection is gone, so a reconnect
    attempt is the only place a listener can learn the device came back in a
    state it cannot be used in.
    """
    listener = MagicMock()
    hub.async_add_connection_listener(listener)
    await drop_connection(hass, ads_client)
    listener.reset_mock()

    ads_client.read_state.side_effect = None
    ads_client.read_state.return_value = (pyads.ADSSTATE_STOP, pyads.ADSSTATE_RUN)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not hub.connected
    assert hub.ads_state == pyads.ADSSTATE_STOP
    listener.assert_called_once()


async def test_a_failed_reconnect_that_changes_nothing_is_quiet(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that stays away does not wake the entities each retry."""
    listener = MagicMock()
    hub.async_add_connection_listener(listener)
    await drop_connection(hass, ads_client)
    listener.reset_mock()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert not hub.connected
    listener.assert_not_called()


async def test_reconnect_resubscribes(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test notifications are registered again on the rebuilt connection.

    The device reissues the handles, so the ones held before the drop are no
    use for routing what arrives after it.
    """
    ads_client.add_device_notification.return_value = (1, 2)
    notification_callback = MagicMock()
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, notification_callback)

    await drop_connection(hass, ads_client)
    ads_client.add_device_notification.return_value = (7, 8)
    await restore_connection(hass, ads_client)

    assert hub.connected
    handler = ads_client.add_device_notification.call_args.args[2]
    handler(build_notification(7, b"\x01"), "GVL.test")

    notification_callback.assert_called_once_with("GVL.test", True)


async def test_subscription_id_survives_a_reconnect(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test an entity can still drop a subscription it made before a drop."""
    ads_client.add_device_notification.return_value = (1, 2)
    subscription = hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())

    await drop_connection(hass, ads_client)
    ads_client.add_device_notification.return_value = (7, 8)
    await restore_connection(hass, ads_client)
    hub.unsubscribe(subscription)

    # The handle from before the drop was released with it; the id the entity
    # holds still reaches the one the device issued on the way back.
    assert ads_client.del_device_notification.call_args_list == [
        call(1, 2),
        call(*STATE_HANDLES),
        call(7, 8),
    ]


async def test_subscribing_while_disconnected_is_deferred(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test an entity added while the device is gone is picked up later."""
    await drop_connection(hass, ads_client)
    ads_client.add_device_notification.reset_mock()

    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())

    ads_client.add_device_notification.assert_not_called()

    ads_client.add_device_notification.return_value = (1, 2)
    await restore_connection(hass, ads_client)

    assert ads_client.add_device_notification.call_args.args[0] == "GVL.test"


async def test_a_drop_survives_a_failing_close(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a connection too far gone to close still gets rebuilt."""
    ads_client.close.side_effect = pyads.ADSError(text="timeout")

    await drop_connection(hass, ads_client)

    assert not hub.connected

    ads_client.close.side_effect = None
    await restore_connection(hass, ads_client)

    assert hub.connected


async def test_reconnect_survives_a_failing_reopen(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device still down when the retry comes round is tried again."""
    await drop_connection(hass, ads_client)
    ads_client.open.side_effect = pyads.ADSError(text="timeout")

    await restore_connection(hass, ads_client)

    assert not hub.connected

    ads_client.open.side_effect = None
    await restore_connection(hass, ads_client, attempt=2)

    assert hub.connected


async def test_reconnect_backs_off(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that stays away is not asked again every few seconds."""
    await drop_connection(hass, ads_client)
    ads_client.open.reset_mock()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ads_client.open.call_count == 1

    # The failed attempt pushed the next one out past the minimum interval.
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ads_client.open.call_count == 1

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=RECONNECT_MIN_INTERVAL * 2 + 1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ads_client.open.call_count == 2


@pytest.mark.parametrize(
    ("plc_datatype", "payload", "expected"),
    [
        pytest.param(pyads.PLCTYPE_BOOL, b"\x01", True, id="bool"),
        pytest.param(pyads.PLCTYPE_INT, struct.pack("<h", -42), -42, id="int"),
        pytest.param(pyads.PLCTYPE_BYTE, struct.pack("<B", 200), 200, id="byte"),
        pytest.param(
            pyads.PLCTYPE_DT, struct.pack("<I", 2**31 + 1), 2**31 + 1, id="dt"
        ),
        pytest.param(pyads.PLCTYPE_TOD, struct.pack("<i", -1), -1, id="tod"),
        pytest.param(pyads.PLCTYPE_REAL, struct.pack("<f", 1.5), 1.5, id="real"),
        pytest.param(pyads.PLCTYPE_STRING, b"hello\x00rest", "hello", id="string"),
        pytest.param(
            pyads.PLCTYPE_LINT, b"\x01\x02", bytearray(b"\x01\x02"), id="unsupported"
        ),
    ],
)
async def test_notification_is_decoded(
    hub: AdsHub,
    ads_client: MagicMock,
    plc_datatype: type,
    payload: bytes,
    expected: Any,
) -> None:
    """Test an incoming notification is decoded for its PLC data type."""
    ads_client.add_device_notification.return_value = (1, 2)
    notification_callback = MagicMock()
    hub.subscribe("GVL.test", plc_datatype, notification_callback)

    # The router calls back into the handler the hub subscribed with.
    handler = ads_client.add_device_notification.call_args.args[2]
    handler(build_notification(1, payload), "GVL.test")

    notification_callback.assert_called_once_with("GVL.test", expected)


async def test_notification_for_unknown_handle(
    hub: AdsHub, ads_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a notification for a handle the hub does not know is logged."""
    ads_client.add_device_notification.return_value = (1, 2)
    hub.subscribe("GVL.test", pyads.PLCTYPE_BOOL, MagicMock())
    handler = ads_client.add_device_notification.call_args.args[2]

    handler(build_notification(99, b"\x01"), "GVL.test")

    assert "Unknown device notification handle: 99" in caplog.text


async def test_notification_timing_is_tuned(hub: AdsHub, ads_client: MagicMock) -> None:
    """Test subscriptions do not ask for pyads' 100 ns defaults."""
    ads_client.add_device_notification.return_value = (1, 2)

    hub.subscribe("GVL.test", pyads.PLCTYPE_INT, MagicMock())

    attr = ads_client.add_device_notification.call_args.args[1]
    # pyads takes milliseconds but reports back 100 ns ticks.
    assert attr.cycle_time == NOTIFICATION_CYCLE_TIME * 1e4
    assert attr.max_delay == NOTIFICATION_MAX_DELAY * 1e4


async def test_setup_subscribes_to_the_device_state(
    hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the device is asked to push its state, not only polled for it."""
    assert hub._state_handles == STATE_HANDLES
    address, attr, _ = ads_client.add_device_notification.call_args[0]
    assert address == DEVICE_STATE_ADDRESS
    assert attr.length == ctypes.sizeof(pyads.PLCTYPE_UINT)


async def test_a_device_that_will_not_push_its_state(
    hass: HomeAssistant, ads_client: MagicMock
) -> None:
    """Test a server that refuses the subscription is still polled."""
    ads_client.add_device_notification.side_effect = pyads.ADSError(text="unsupported")
    hub = AdsHub(hass, ads_client)

    await hub.async_setup()

    assert hub.connected
    assert hub._state_handles is None

    await drop_connection(hass, ads_client)
    assert not hub.connected

    await hub.async_shutdown()


async def test_a_pushed_stop_drops_the_connection(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a state change lands without waiting for the next probe."""
    listener = MagicMock()
    hub.async_add_connection_listener(listener)

    await push_state(hass, ads_client, pyads.ADSSTATE_STOP)

    assert not hub.connected
    assert hub.ads_state == pyads.ADSSTATE_STOP
    assert hub.reachable
    assert listener.called
    # Only the one setup made: the drop did not need a probe of its own.
    ads_client.read_state.assert_called_once()


async def test_a_pushed_run_reconnects_without_the_backoff(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test a device that says it is running again is not left waiting."""
    await push_state(hass, ads_client, pyads.ADSSTATE_STOP)
    assert not hub.connected

    await push_state(hass, ads_client, pyads.ADSSTATE_RUN)
    # Far short of the pending 5 s retry, which the push brought forward.
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hub.connected


async def test_a_stopped_device_keeps_pushing(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the state notification survives a drop that keeps the socket.

    It is the only thing that can announce the restart, so releasing it would
    put recovery back on the reconnect backoff.
    """
    await push_state(hass, ads_client, pyads.ADSSTATE_STOP)

    assert hub._state_handles == STATE_HANDLES
    assert call(*STATE_HANDLES) not in ads_client.del_device_notification.mock_calls


async def test_a_silent_device_loses_and_regains_the_state_notification(
    hass: HomeAssistant, hub: AdsHub, ads_client: MagicMock
) -> None:
    """Test the notification is rebuilt with the socket it was made on."""
    await drop_connection(hass, ads_client)

    assert hub._state_handles is None
    assert call(*STATE_HANDLES) in ads_client.del_device_notification.mock_calls

    await restore_connection(hass, ads_client)

    assert hub._state_handles == STATE_HANDLES
