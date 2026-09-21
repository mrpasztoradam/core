"""Test the ADS component setup."""

from unittest.mock import MagicMock

import pyads
import pytest

from homeassistant.components.ads import (
    CONF_ADS_TYPE,
    CONF_ADS_VALUE,
    SERVICE_WRITE_DATA_BY_NAME,
)
from homeassistant.components.ads.const import CONF_ADS_VAR, DATA_ADS, DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.setup import async_setup_component

from . import ADS_CONFIG
from .const import AMS_NET_ID, IP_ADDRESS, PORT

from tests.common import MockUser


async def test_setup(hass: HomeAssistant, mock_pyads_connection: MagicMock) -> None:
    """Test the component opens the connection it was configured with."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()

    mock_pyads_connection.assert_called_once_with(AMS_NET_ID, PORT, IP_ADDRESS)
    mock_pyads_connection.return_value.open.assert_called_once()
    assert hass.data[DATA_ADS]


async def test_setup_without_config(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a platform pulling the component in without a config section."""
    # The sensor domain itself still sets up; only its ads platform cannot.
    assert await async_setup_component(
        hass, SENSOR_DOMAIN, {SENSOR_DOMAIN: {"platform": DOMAIN, "adsvar": "GVL.x"}}
    )
    await hass.async_block_till_done()

    assert "need an 'ads' section in configuration.yaml" in caplog.text
    assert DATA_ADS not in hass.data
    mock_pyads_connection.return_value.open.assert_not_called()


async def test_setup_connection_error(
    hass: HomeAssistant, mock_pyads_connection: MagicMock
) -> None:
    """Test a connection error leaves the component unset up."""
    mock_pyads_connection.return_value.open.side_effect = pyads.ADSError(text="timeout")

    assert not await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()

    assert DATA_ADS not in hass.data


async def test_shutdown_on_stop(
    hass: HomeAssistant, mock_pyads_connection: MagicMock
) -> None:
    """Test the connection is closed when Home Assistant stops."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    mock_pyads_connection.return_value.close.assert_called_once()


@pytest.mark.parametrize(
    ("ads_type", "value", "written", "plc_datatype"),
    [
        pytest.param("int", 42, 42, pyads.PLCTYPE_INT, id="int"),
        pytest.param("int", "42", 42, pyads.PLCTYPE_INT, id="int_as_text"),
        pytest.param("real", 21.5, 21.5, pyads.PLCTYPE_REAL, id="real"),
        pytest.param("real", "21.5", 21.5, pyads.PLCTYPE_REAL, id="real_as_text"),
        pytest.param("lreal", "21.5", 21.5, pyads.PLCTYPE_LREAL, id="lreal_as_text"),
        pytest.param("bool", "true", True, pyads.PLCTYPE_BOOL, id="bool_as_text"),
        pytest.param("string", "hello", "hello", pyads.PLCTYPE_STRING, id="string"),
    ],
)
async def test_write_data_by_name(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    ads_type: str,
    value: bool | float | str,
    written: bool | float | str,
    plc_datatype: type,
) -> None:
    """Test the write action writes the value to the PLC as its ADS type."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_WRITE_DATA_BY_NAME,
        {CONF_ADS_VAR: "GVL.setpoint", CONF_ADS_TYPE: ads_type, CONF_ADS_VALUE: value},
        blocking=True,
    )

    mock_pyads_connection.return_value.write_by_name.assert_called_once_with(
        "GVL.setpoint", written, plc_datatype
    )


async def test_write_data_by_name_requires_admin(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    hass_read_only_user: MockUser,
) -> None:
    """Test a non-admin user cannot write to the PLC."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_WRITE_DATA_BY_NAME,
            {CONF_ADS_VAR: "GVL.setpoint", CONF_ADS_TYPE: "int", CONF_ADS_VALUE: 42},
            blocking=True,
            context=Context(user_id=hass_read_only_user.id),
        )

    mock_pyads_connection.return_value.write_by_name.assert_not_called()


async def test_write_data_by_name_error(
    hass: HomeAssistant,
    mock_pyads_connection: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failing write is logged instead of raised at the caller."""
    assert await async_setup_component(hass, DOMAIN, {DOMAIN: ADS_CONFIG})
    await hass.async_block_till_done()
    mock_pyads_connection.return_value.write_by_name.side_effect = pyads.ADSError(
        text="timeout"
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_WRITE_DATA_BY_NAME,
        {CONF_ADS_VAR: "GVL.setpoint", CONF_ADS_TYPE: "int", CONF_ADS_VALUE: 42},
        blocking=True,
    )

    assert "Error writing GVL.setpoint" in caplog.text
