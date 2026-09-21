"""Common fixtures for the ADS tests."""

from collections.abc import Callable, Generator
from itertools import count
import struct
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pyads
import pytest

from homeassistant.components.ads.hub import (
    ADSIGRP_DEVICE_DATA,
    ADSIOFFS_DEVDATA_ADSSTATE,
)

from . import build_notification
from .const import AMS_NET_ID, PORT, STATE_HANDLES

DEVICE_STATE_ADDRESS = (ADSIGRP_DEVICE_DATA, ADSIOFFS_DEVDATA_ADSSTATE)


@pytest.fixture
def mock_pyads_connection() -> Generator[MagicMock]:
    """Mock the pyads Connection class."""
    with patch("pyads.Connection", autospec=True) as mock_connection:
        mock_connection.return_value.ams_netid = AMS_NET_ID
        mock_connection.return_value.ams_port = PORT
        mock_connection.return_value.read_state.return_value = (
            pyads.ADSSTATE_RUN,
            pyads.ADSSTATE_RUN,
        )
        yield mock_connection


@pytest.fixture
def mock_ads_notifications(
    mock_pyads_connection: MagicMock,
) -> dict[str, bytes]:
    """Push an initial value for every subscription, the way a PLC would.

    Yields a mapping of ADS variable name to the raw bytes to deliver, to be
    filled in before the entities are set up. Variables left out get zeroes.
    """
    values: dict[str, bytes] = {}
    handles = count(1)

    def _add_device_notification(
        name: str | tuple[int, int],
        attr: pyads.NotificationAttrib,
        callback: Callable[[Any, str], None],
    ) -> tuple[int, int]:
        if name == DEVICE_STATE_ADDRESS:
            mock_pyads_connection.return_value.state_callback = callback
            handle = STATE_HANDLES[0]
            state = mock_pyads_connection.return_value.read_state.return_value[0]
            payload = struct.pack("<H", state)
        else:
            handle = next(handles)
            payload = values.get(name, b"").ljust(attr.length, b"\x00")
        # The hub registers the handle before releasing the lock the callback
        # takes, so the delivery cannot run ahead of the registration.
        threading.Thread(
            target=callback,
            args=(build_notification(handle, payload), name),
            daemon=True,
        ).start()
        return handle, handle

    mock_pyads_connection.return_value.add_device_notification.side_effect = (
        _add_device_notification
    )
    return values
