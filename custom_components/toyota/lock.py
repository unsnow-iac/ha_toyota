"""Remote door lock for Toyota Connected Services.

Exposes the car's remote door lock/unlock (``DOOR_LOCK`` / ``DOOR_UNLOCK``
remote commands) as a Home Assistant ``lock`` entity. State is read back from
``vehicle.lock_status`` (the same source the door binary_sensors use); the
command is sent via ``vehicle.post_command``.

Toyota's lock telemetry is slow and stale — after a successful remote
command the ``/status`` endpoint keeps reporting the OLD state for minutes
until the car's modem gets around to reporting the change. So this is an
``assumed_state`` lock: once a command is sent it optimistically shows the
commanded state and holds it, rather than snapping back to stale telemetry.
The optimistic value is only released once telemetry reports a reading that
has actually *moved* from what it was when the command went out, at which
point we trust the real data again.
"""

# pylint: disable=W0212, W0718

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.core import callback
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

# Seconds to wait after a command before nudging a re-poll. Toyota rarely
# reflects the change this fast, but it kicks off the reconciliation that
# eventually releases the optimistic state once fresh telemetry lands.
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

    # Toyota telemetry lags the remote command by minutes; show what we
    # commanded rather than the stale reported state.
    _attr_assumed_state = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Initialize, with no optimistic command outstanding."""
        super().__init__(*args, **kwargs)
        # Commanded state we are asserting until telemetry catches up; None
        # means trust the reported state.
        self._optimistic_locked: bool | None = None
        # Telemetry reading captured when the command was sent, used to detect
        # when the reported state has genuinely moved.
        self._telemetry_at_command: bool | None = None

    @property
    def is_locked(self) -> bool | None:
        """Return the optimistic commanded state, else the reported state."""
        if self._optimistic_locked is not None:
            return self._optimistic_locked
        return _doors_locked(self.vehicle)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Release the optimistic state once telemetry has actually moved."""
        if self._optimistic_locked is not None:
            reported = _doors_locked(self.coordinator.data[self.index]["data"])
            if reported != self._telemetry_at_command:
                # Fresh data that differs from the pre-command reading: the
                # car has reported the change, trust telemetry again.
                self._optimistic_locked = None
                self._telemetry_at_command = None
        super()._handle_coordinator_update()

    async def async_lock(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Lock the doors."""
        await self._send_command(CommandType.DOOR_LOCK, locking=True)

    async def async_unlock(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Unlock the doors."""
        await self._send_command(CommandType.DOOR_UNLOCK, locking=False)

    async def _send_command(self, command: CommandType, *, locking: bool) -> None:
        """Send a remote door command and hold the commanded state optimistically."""
        # Capture the reported state now, so we can later tell when it moves.
        self._telemetry_at_command = _doors_locked(self.vehicle)
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
                # Command rejected: don't assert a state we couldn't set.
                self._telemetry_at_command = None
                return
            # Command accepted: assert the commanded state until telemetry
            # catches up (which can take minutes on Toyota's side).
            self._optimistic_locked = locking
            # Nudge a re-poll to kick off reconciliation; the optimistic value
            # protects against the stale reading this will likely return.
            await asyncio.sleep(_REFRESH_DELAY)
            await self.coordinator.async_request_refresh()
        except Exception:  # pylint: disable=W0718
            _LOGGER.exception(
                "Error sending %s to %s", command.value, self.vehicle.alias
            )
            self._optimistic_locked = None
            self._telemetry_at_command = None
        finally:
            self._attr_is_locking = False
            self._attr_is_unlocking = False
            self.async_write_ha_state()
