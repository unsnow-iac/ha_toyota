"""Remote door lock for Toyota Connected Services.

Exposes the car's remote door lock/unlock (``DOOR_LOCK`` / ``DOOR_UNLOCK``
remote commands) as a Home Assistant ``lock`` entity. State is read back from
``vehicle.lock_status`` (the same source the door binary_sensors use); the
command is sent via ``vehicle.post_command`` and a coordinator refresh is
requested so the reported state catches up.
"""

# pylint: disable=W0212, W0718

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.lock import LockEntity, LockEntityDescription
from pytoyoda.models.endpoints.command import CommandType

from .const import DOMAIN, ICON_CAR_DOOR_LOCK
from .entity import ToyotaBaseEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from pytoyoda.models.vehicle import Vehicle

    from . import VehicleData

_LOGGER = logging.getLogger(__name__)

# Cabin doors the DOOR_LOCK/DOOR_UNLOCK command operates on. The trunk and hood
# have their own commands/state and are intentionally excluded here.
_DOOR_ATTRS = (
    "driver_seat",
    "driver_rear_seat",
    "passenger_seat",
    "passenger_rear_seat",
)

# Seconds to wait after a command before re-polling, giving the car's modem a
# moment to report the new lock state.
_REFRESH_DELAY = 3

LOCK_DESCRIPTION = LockEntityDescription(
    key="door_lock",
    translation_key="door_lock",
    name="Door lock",
    icon=ICON_CAR_DOOR_LOCK,
)


def _doors_locked(vehicle: Vehicle) -> bool | None:
    """Aggregate cabin-door lock state.

    Returns True when no door is reported unlocked and at least one is reported
    locked; False if any door is reported unlocked; None when nothing is known
    (Toyota only reports the doors it has fresh data for).
    """
    doors = getattr(vehicle.lock_status, "doors", None)
    states = [
        getattr(getattr(doors, attr, None), "locked", None) for attr in _DOOR_ATTRS
    ]
    if any(state is False for state in states):
        return False
    if any(state is True for state in states):
        return True
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Toyota door lock entity."""
    coordinator: DataUpdateCoordinator[list[VehicleData]] = hass.data[DOMAIN][
        entry.entry_id
    ]
    async_add_entities(
        ToyotaDoorLock(
            coordinator=coordinator,
            entry_id=entry.entry_id,
            vehicle_index=index,
            description=LOCK_DESCRIPTION,
        )
        for index in range(len(coordinator.data))
    )


class ToyotaDoorLock(ToyotaBaseEntity, LockEntity):
    """Remote door lock/unlock for one VIN."""

    @property
    def is_locked(self) -> bool | None:
        """Return True if all reporting cabin doors are locked."""
        return _doors_locked(self.vehicle)

    async def async_lock(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Lock the doors."""
        await self._send_command(CommandType.DOOR_LOCK, locking=True)

    async def async_unlock(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Unlock the doors."""
        await self._send_command(CommandType.DOOR_UNLOCK, locking=False)

    async def _send_command(self, command: CommandType, *, locking: bool) -> None:
        """Send a remote door command, then refresh so reported state catches up."""
        # Show the in-progress UI state while the command is in flight.
        self._attr_is_locking = locking
        self._attr_is_unlocking = not locking
        self.async_write_ha_state()
        try:
            _LOGGER.debug("Sending %s to %s", command.value, self.vehicle.alias)
            status = await self.vehicle.post_command(command)
            code = getattr(status, "code", None)
            if code is not None and code >= 400:
                _LOGGER.warning(
                    "%s for %s returned code %s: %s",
                    command.value,
                    self.vehicle.alias,
                    code,
                    getattr(status, "message", None),
                )
            # Give the modem a moment, then re-poll the real lock state.
            await asyncio.sleep(_REFRESH_DELAY)
            await self.coordinator.async_request_refresh()
        except Exception:  # pylint: disable=W0718
            _LOGGER.exception(
                "Error sending %s to %s", command.value, self.vehicle.alias
            )
        finally:
            self._attr_is_locking = False
            self._attr_is_unlocking = False
            self.async_write_ha_state()
