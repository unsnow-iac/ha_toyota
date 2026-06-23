"""Toyota button entities.

Per-vehicle buttons:

* ``refresh_vehicle_status`` — one-tap wrapper around the
  ``toyota.refresh_vehicle_status`` service (wake POST + status poll).
* ``buzzer`` — fires the ``BUZZER_WARNING`` remote command (sounds the car's
  locator buzzer).
* ``hazard`` — fires the ``HAZARD_ON`` remote command (flashes the hazard
  lights, which the car then turns off again on its own).

The buzzer and hazard buttons are the "find my car" primitives on this
platform. They are momentary fire-and-forget actions with no state to track
(hazard self-stops; there is no working HAZARD_OFF), so they are buttons
rather than switches. Compose them into a combined "find my car" action with
a Home Assistant script if you want buzzer + flash together.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from pytoyoda.models.endpoints.command import CommandType

from .const import DOMAIN, HTTP_ERROR_THRESHOLD
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

BUZZER_BUTTON_DESCRIPTION = ButtonEntityDescription(
    key="buzzer",
    translation_key="buzzer",
    name="Buzzer",
    icon="mdi:bullhorn",
)

HAZARD_BUTTON_DESCRIPTION = ButtonEntityDescription(
    key="hazard",
    translation_key="hazard",
    name="Hazard lights",
    icon="mdi:hazard-lights",
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
        common = {
            "coordinator": coordinator,
            "entry_id": entry.entry_id,
            "vehicle_index": index,
        }
        entities.append(
            ToyotaRefreshStatusButton(description=REFRESH_BUTTON_DESCRIPTION, **common)
        )
        entities.append(
            ToyotaBuzzerButton(description=BUZZER_BUTTON_DESCRIPTION, **common)
        )
        entities.append(
            ToyotaHazardButton(description=HAZARD_BUTTON_DESCRIPTION, **common)
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


class ToyotaRemoteCommandButton(ToyotaBaseEntity, ButtonEntity):
    """Base for buttons that fire a single fire-and-forget remote command.

    Subclasses set ``_command``. Toyota signals failure either by raising
    (e.g. an unsupported command 400s) or, less often, by a >=400 ``code`` on
    the returned status; both are logged. A successful command returns a
    status whose ``code`` is None, so we do not treat None as an error.
    """

    _command: CommandType

    async def async_press(self) -> None:
        """Send this button's remote command to the car."""
        command = self._command
        try:
            _LOGGER.debug("Sending %s to %s", command.value, self.vehicle.alias)
            status = await self.vehicle.post_command(command)
        except Exception:  # pylint: disable=W0718
            _LOGGER.exception(
                "Error sending %s to %s", command.value, self.vehicle.alias
            )
            return
        code = getattr(status, "code", None)
        if code is not None and code >= HTTP_ERROR_THRESHOLD:
            _LOGGER.warning(
                "%s for %s returned code %s: %s",
                command.value,
                self.vehicle.alias,
                code,
                getattr(status, "message", None),
            )


class ToyotaBuzzerButton(ToyotaRemoteCommandButton):
    """Sound the car's locator buzzer (BUZZER_WARNING)."""

    _command = CommandType.BUZZER_WARNING


class ToyotaHazardButton(ToyotaRemoteCommandButton):
    """Flash the hazard lights (HAZARD_ON; the car turns them off itself)."""

    _command = CommandType.HAZARD_ON
