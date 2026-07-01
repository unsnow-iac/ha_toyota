"""Toyota Connected Services writable climate switches."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityDescription

from .climate import ToyotaClimate, _vehicle_has_climate_capability
from .const import DOMAIN
from .entity import ToyotaBaseEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Toyota climate switches."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    description = EntityDescription(
        key="steering_heater",
        name="Steering heater",
        icon="mdi:steering",
    )

    entities = []
    for index, vehicle_data in enumerate(coordinator.data):
        # Gate on climate capability, NOT the steering-heater capability flag:
        # Toyota reports extended_capabilities.steering_heater=False even on cars
        # that physically have (and remotely honor) a heated wheel, so the flag
        # cannot be trusted to detect presence.
        if _vehicle_has_climate_capability(vehicle_data["data"]):
            entities.append(
                ToyotaSteeringHeaterSwitch(
                    coordinator, entry.entry_id, index, description
                )
            )
    async_add_entities(entities)


class ToyotaSteeringHeaterSwitch(ToyotaBaseEntity, SwitchEntity):
    """Writable steering-wheel heater.

    A thin control over ``ToyotaClimate``: the climate entity owns the
    climate-control request, so this records the desired value there and reads it
    back. Store-only — applied on the next climate START (never actuates on its own
    and never starts the engine). Note the wheel only physically heats while the
    climate is HEATING (a low target temp = cooling suppresses it), and the car
    exposes no read-back, so this switch is an optimistic desired-state control.
    """

    def _climate(self) -> ToyotaClimate | None:
        """The sibling climate entity for this vehicle, if registered."""
        registry = getattr(self.coordinator, "climate_entity_by_index", None)
        if registry is None:
            return None
        return registry.get(self.index)

    @property
    def is_on(self) -> bool | None:
        """Whether the steering heater is (desired) on. None if unknown yet."""
        climate = self._climate()
        if climate is None:
            return None
        desired = climate.steering_heater_desired
        if desired is None:
            return None
        return desired == "on"

    # kwargs is required by the SwitchEntity.async_turn_on/off contract but unused.
    async def async_turn_on(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Turn the steering heater on."""
        await self._set(on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:  # noqa: ANN401, ARG002
        """Turn the steering heater off."""
        await self._set(on=False)

    async def _set(self, *, on: bool) -> None:
        climate = self._climate()
        if climate is None:
            _LOGGER.debug("Climate entity not available; cannot set steering heater")
            return
        await climate.async_set_steering_heater(on=on)
        self.async_write_ha_state()
