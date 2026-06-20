"""Toyota button entities.

Two per-vehicle buttons:

* ``refresh_vehicle_status`` — one-tap wrapper around the
  ``toyota.refresh_vehicle_status`` service (wake POST + status poll).
* ``find_vehicle`` — fires the ``FIND_VEHICLE`` remote command, which makes
  the car flash its lights / sound its buzzer so you can locate it nearby.
  Momentary action with no state to track, so it is a button rather than a
  switch. (The "where did I park" map view is already covered by the
  device_tracker entity; this is the close-range "make the car announce
  itself" signal.)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from pytoyoda.models.endpoints.command import CommandType

from .const import DOMAIN
from .entity import ToyotaBaseEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from . import VehicleData

_LOGGER = logging.getLogger(__name__)


REFRESH_BUTTON_DESCRIPTION = ButtonEntityDescription(
    key="refresh_vehicle_status",
    translation_key="refresh_vehicle_status",
    name="Refresh vehicle status",
    icon="mdi:refresh-circle",
)

FIND_VEHICLE_BUTTON_DESCRIPTION = ButtonEntityDescription(
    key="find_vehicle",
    translation_key="find_vehicle",
    name="Find vehicle",
    icon="mdi:car-search",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Toyota button entities."""
    coordinator: DataUpdateCoordinator[list[VehicleData]] = hass.data[DOMAIN][
        entry.entry_id
    ]
    entities: list[ButtonEntity] = []
    for index in range(len(coordinator.data)):
        entities.append(
            ToyotaRefreshStatusButton(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                vehicle_index=index,
                description=REFRESH_BUTTON_DESCRIPTION,
            )
        )
        entities.append(
            ToyotaFindVehicleButton(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                vehicle_index=index,
                description=FIND_VEHICLE_BUTTON_DESCRIPTION,
            )
        )
    async_add_entities(entities)


class ToyotaRefreshStatusButton(ToyotaBaseEntity, ButtonEntity):
    """One-tap wrapper around toyota.refresh_vehicle_status for one VIN."""

    async def async_press(self) -> None:
        """Fire toyota.refresh_vehicle_status for this vehicle's device."""
        from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

        device_reg = dr.async_get(self.hass)
        device = device_reg.async_get_device(
            identifiers={(DOMAIN, self.vehicle.vin or "")}
        )
        if device is None:
            return
        await self.hass.services.async_call(
            DOMAIN,
            "refresh_vehicle_status",
            {"device_id": [device.id]},
            blocking=False,
        )


class ToyotaFindVehicleButton(ToyotaBaseEntity, ButtonEntity):
    """Fire the FIND_VEHICLE remote command (flash/buzzer) for one VIN."""

    async def async_press(self) -> None:
        """Send the FIND_VEHICLE remote command to the car."""
        command = CommandType.FIND_VEHICLE
        try:
            _LOGGER.debug("Sending %s to %s", command.value, self.vehicle.alias)
            status = await self.vehicle.post_command(command)
        except Exception:  # noqa: BLE001  # pylint: disable=W0718
            _LOGGER.exception(
                "Error sending %s to %s", command.value, self.vehicle.alias
            )
            return
        code = getattr(status, "code", None)
        if code is not None and code >= 400:
            _LOGGER.warning(
                "%s for %s returned code %s: %s",
                command.value,
                self.vehicle.alias,
                code,
                getattr(status, "message", None),
            )
