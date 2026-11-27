"""Support for Automation Device Specification (ADS)."""

import asyncio
from asyncio import timeout
from collections.abc import Callable
import logging
from typing import Any, override

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import STATE_KEY_STATE
from .hub import AdsHub

_LOGGER = logging.getLogger(__name__)


class AdsEntity(Entity):
    """Representation of ADS entity."""

    _attr_should_poll = False

    def __init__(
        self,
        ads_hub: AdsHub,
        name: str,
        ads_var: str,
        *,
        unique_id: str | None = None,
        device_info: DeviceInfo | None = None,
        entity_name: str | None = None,
        entity_category: EntityCategory | None = None,
    ) -> None:
        """Initialize the entity.

        A YAML entity is named by its config and identified by its variable. An
        entity of a subentry passes the subentry's device and ID instead, and
        takes the device's name.
        """
        self._state_dict: dict[str, Any] = {}
        self._state_dict[STATE_KEY_STATE] = None
        self._ads_hub = ads_hub
        self._ads_var = ads_var
        self._notification_handles: list[int] = []
        self._removed = False
        self._subscribe_task: asyncio.Task[None] | None = None
        # An entity added in the UI is added without waiting for its first
        # value. Waiting holds up the entry's setup, and an entity whose device
        # is deleted meanwhile leaves its state behind once the add completes.
        self._subscribe_in_background = device_info is not None
        if device_info is None:
            self._attr_unique_id = ads_var
            self._attr_name = name
        else:
            self._attr_unique_id = unique_id
            self._attr_has_entity_name = True
            # None names the entity after its device.
            self._attr_name = entity_name
            self._attr_device_info = device_info
            self._attr_entity_category = entity_category
        ads_hub.register_device(self)
        # Also runs when the entity platform refuses to add this entity, so a
        # rejected one is not left behind on the hub.
        self.async_on_remove(self._unregister_from_hub)

    @override
    async def async_added_to_hass(self) -> None:
        """Subscribe once the entity is added."""
        if self._subscribe_in_background:
            # Removing the entity drops a subscription still in flight.
            self.hass.async_create_task(
                self.async_run_subscribe(), f"ads subscribe {self._ads_var}"
            )
            return
        await self.async_run_subscribe()

    async def async_run_subscribe(self) -> None:
        """Run async_subscribe in a task that a rebind to a new hub cancels.

        Otherwise a subscription still waiting for a first value would carry on
        against the new hub next to its replacement and subscribe twice.
        """
        self._subscribe_task = task = self.hass.async_create_task(
            self.async_subscribe(), f"ads subscribe {self._ads_var}"
        )
        try:
            await task
        except asyncio.CancelledError:
            # Only swallow the cancellation of the subscription itself.
            if (current := asyncio.current_task()) and current.cancelling():
                raise

    async def async_subscribe(self) -> None:
        """Subscribe to the PLC variables of this entity.

        Also called after the entry is reloaded, to subscribe again on the new
        hub.
        """
        raise NotImplementedError

    async def async_initialize_device(
        self,
        ads_var: str,
        plctype: type,
        state_key: str = STATE_KEY_STATE,
        factor: int | None = None,
    ) -> None:
        """Register device notification."""

        def update(name, value):
            """Handle device notifications."""
            _LOGGER.debug("Variable %s changed its value to %d", name, value)

            if factor is None:
                self._state_dict[state_key] = value
            else:
                self._state_dict[state_key] = value / factor

            # Callbacks arrive on a pyads thread, so hop to the event loop.
            self.hass.loop.call_soon_threadsafe(event.set)
            self.schedule_update_ha_state()

        event = asyncio.Event()

        if not await self.async_add_notification(ads_var, plctype, update):
            return
        try:
            async with timeout(10):
                await event.wait()
        except TimeoutError:
            _LOGGER.debug("Variable %s: Timeout during first update", ads_var)
        else:
            return
        # The PLC sends the current value as soon as the notification is added,
        # and the ADS library drops it if it arrives before the library has
        # registered the handle. Later changes still arrive, so read the value
        # once instead of staying unavailable until the variable changes.
        if self._removed:
            return
        value = await self.hass.async_add_executor_job(
            self._ads_hub.read_by_name, ads_var, plctype
        )
        if value is None or self._removed or self._state_dict[state_key] is not None:
            return
        self._state_dict[state_key] = value if factor is None else value / factor
        self.async_write_ha_state()

    async def async_add_notification(
        self,
        ads_var: str,
        plctype: type,
        notification_callback: Callable[[str, Any], None],
    ) -> bool:
        """Subscribe to a variable for this entity, returning whether it worked.

        The handle is tracked, so removing the entity deletes it again.
        """
        handle = await self.hass.async_add_executor_job(
            self._ads_hub.add_device_notification,
            ads_var,
            plctype,
            notification_callback,
        )
        if handle is None:
            # No notification is registered, so no first update will arrive.
            return False
        if self._removed:
            # Removed while this was subscribing, so the removal has already
            # drained the handles and will not come back for this one.
            await self.hass.async_add_executor_job(
                self._ads_hub.delete_device_notification, handle
            )
            return False
        self._notification_handles.append(handle)
        return True

    @property
    @override
    def available(self) -> bool:
        """Return False if state has not been updated yet."""
        return self._state_dict[STATE_KEY_STATE] is not None

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Drop the subscriptions this entity added.

        The hub holds the callback, so leaving them behind would keep the PLC
        pushing values for a variable nobody reads and pin this entity.
        """
        # Set before the first await, so a subscription still in flight sees it
        # and cleans up after itself.
        self._removed = True
        handles = self._notification_handles
        self._notification_handles = []
        await self.hass.async_add_executor_job(
            self._ads_hub.delete_device_notifications, handles
        )

    def mark_unavailable(self) -> None:
        """Mark the entity unavailable after its hub connection is closed."""
        # Notification callbacks add keys from another thread, so snapshot them.
        for key in list(self._state_dict):
            self._state_dict[key] = None
        if self.hass is not None:
            self.schedule_update_ha_state()

    @callback
    def _unregister_from_hub(self) -> None:
        """Unregister this entity from its hub."""
        self._ads_hub.unregister_device(self)

    def rebind(self, ads_hub: AdsHub) -> None:
        """Rebind this entity to a new hub after the entry is reloaded."""
        if self._subscribe_task is not None:
            self._subscribe_task.cancel()
            self._subscribe_task = None
        self._ads_hub = ads_hub
        # The old hub's handles died with its connection, and pyads can hand
        # out the same numbers again on the new one.
        self._notification_handles.clear()
        ads_hub.register_device(self)
