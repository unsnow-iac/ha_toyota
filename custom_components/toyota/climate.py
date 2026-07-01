"""Toyota Connected Services Climate Control."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.event import async_call_later
from pytoyoda.models.endpoints.climate import (
    ACOperations,
    ACParameters,
    ClimateControlModel,
    ClimateSettingsRequestModel,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
    from pytoyoda.models.vehicle import Vehicle

from .const import DOMAIN
from .entity import ToyotaBaseEntity

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=120)

# Debounce delay for API calls (in seconds)
SETTINGS_DEBOUNCE_DELAY = 5.0

# The /v1/vehicle/climate-settings payload no longer carries min/max/step; mirror
# the MyToyota app's fixed 18-29 degree range with a 1-degree step.
DEFAULT_MIN_TEMP = 18
DEFAULT_MAX_TEMP = 29
DEFAULT_TEMP_STEP = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Toyota climate entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    description = EntityDescription(
        key="climate",
        name="Climate",
    )

    entities = []
    for index, vehicle_data in enumerate(coordinator.data):
        if _vehicle_has_climate_capability(vehicle_data["data"]):
            entities.append(
                ToyotaClimate(coordinator, entry.entry_id, index, description)
            )
    async_add_entities(entities)


def _vehicle_has_climate_capability(vehicle: Vehicle) -> bool:
    """Check if vehicle supports climate control."""
    try:
        # Standard path (ICE / hybrid, e.g. Corolla): legacy feature flag.
        if getattr(
            getattr(vehicle._vehicle_info, "features", False),  # noqa : SLF001
            "climate_start_engine",
            False,
        ):
            return True
        # PHEV / EV path: extended capabilities (added upstream in ea73031).
        caps = getattr(vehicle._vehicle_info, "extended_capabilities", False)  # noqa : SLF001
        for cap in [
            "climate_capable",
            "econnect_climate_capable",
            "remote_engine_start_stop",
        ]:
            if getattr(caps, cap, False):
                return True
    except Exception:  # pylint: disable=W0718 # noqa : BLE001
        return False

    return False


class ToyotaClimate(ToyotaBaseEntity, ClimateEntity):
    """Representation of a Toyota climate control."""

    _attr_translation_key = "climate"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = (HVACMode.OFF, HVACMode.HEAT_COOL)
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.PRESET_MODE
    )

    _attr_preset_modes = ("none", "front_defrost", "rear_defrost", "both_defrost")

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        entry_id: str,
        vehicle_index: int,
        description: EntityDescription,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, entry_id, vehicle_index, description)

        # Initialize with defaults first
        self._attr_target_temperature = 21
        self._attr_min_temp = 18
        self._attr_max_temp = 29
        self._attr_target_temperature_step = 1
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_front_defrost = False
        self._attr_rear_defrost = False
        self._attr_current_temperature = None
        self._attr_climate_status = False

        # Debouncing state - using HA's task cancellation
        self._pending_settings_cancel = None
        self._settings_changed = False

        # Load settings from coordinator if available
        self._load_climate_settings_from_coordinator()

    def _load_climate_settings_from_coordinator(self) -> None:
        """Load climate settings from coordinator data if available."""
        try:
            if not self.vehicle or not getattr(self.vehicle, "climate_settings", None):
                _LOGGER.debug("Vehicle climate_settings not yet available")
                return

            # Update temperature settings
            self._load_temperature_settings()

            # Read defrost settings from operations
            self._load_defrost_settings()

            _LOGGER.debug(
                "Loaded climate settings for %s: temp=%s, min=%s, max=%s",
                getattr(self.vehicle, "alias", "vehicle"),
                self._attr_target_temperature,
                self._attr_min_temp,
                self._attr_max_temp,
            )
        except Exception:  # pylint: disable=W0718
            _LOGGER.exception("Error loading climate settings from coordinator")

    def _load_temperature_settings(self) -> None:
        """Load target temperature + unit from climate_settings."""
        climate_settings = self.vehicle.climate_settings
        target_temperature = climate_settings.temperature
        if target_temperature is not None and target_temperature.value is not None:
            self._attr_target_temperature = target_temperature.value
            # Honor the unit the car reports rather than assuming Celsius.
            unit = (target_temperature.unit or "").upper()
            self._attr_temperature_unit = (
                UnitOfTemperature.FAHRENHEIT
                if unit.startswith("F")
                else UnitOfTemperature.CELSIUS
            )
        # The new climate-settings payload no longer carries min/max/step; use the
        # app's fixed bounds (a None min/max would make HA core's set_temperature
        # validation do `float < None` -> TypeError).
        self._attr_min_temp = DEFAULT_MIN_TEMP
        self._attr_max_temp = DEFAULT_MAX_TEMP
        self._attr_target_temperature_step = DEFAULT_TEMP_STEP

    def _load_defrost_settings(self) -> None:
        """Load defrost/defogger state from climate_settings heating options."""
        # Migrated 2026-07: defrost state moved from the old acOperations list to
        # the new heatingOptions map. heating_options can be None (climate-settings
        # 403/500), so guard before reading.
        heating = getattr(self.vehicle.climate_settings, "heating_options", None)
        if heating is None:
            return
        if heating.front_defroster is not None:
            self._attr_front_defrost = heating.front_defroster
        if heating.rear_defogger is not None:
            self._attr_rear_defrost = heating.rear_defogger

    def _load_climate_status_from_coordinator(self) -> None:
        """Reflect the car's live climate state (on/off + cabin temp) if available.

        The coordinator fetches climate_status each cycle; reading it here keeps the
        entity truthful even when climate is started/stopped from the Toyota app.
        """
        try:
            climate_status = getattr(self.vehicle, "climate_status", None)
            if climate_status is None:
                return
            is_on = climate_status.is_on
            if is_on is not None:
                self._attr_climate_status = is_on
                self._attr_hvac_mode = (
                    HVACMode.HEAT_COOL if is_on else HVACMode.OFF
                )
            current = climate_status.current_temperature
            self._attr_current_temperature = (
                current.value if current is not None else None
            )
        except Exception:  # pylint: disable=W0718
            _LOGGER.exception("Error loading climate status from coordinator")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Surface the new climate capabilities (read-only until the V2 PR).

        Seat heaters are multi-level (off/low/medium/high); steering heater and the
        defroster/defogger are on/off. These are not yet writable entities.
        """
        settings = getattr(self.vehicle, "climate_settings", None)
        if settings is None:
            return None
        heating = settings.heating_options
        seats = settings.seat_options
        attrs: dict[str, Any] = {}
        if heating is not None:
            attrs["steering_heater"] = heating.steering_heater
        if seats is not None:
            attrs["seat_heater_driver"] = seats.driver_seat
            attrs["seat_heater_passenger"] = seats.passenger_seat
            attrs["seat_heater_rear_driver"] = seats.rear_driver_seat
            attrs["seat_heater_rear_passenger"] = seats.rear_passenger_seat
        if settings.duration is not None:
            attrs["duration_minutes"] = int(
                settings.duration.total_seconds() // 60
            )
        return attrs or None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._load_climate_settings_from_coordinator()
        self._load_climate_status_from_coordinator()
        super()._handle_coordinator_update()

    @property
    def should_poll(self) -> bool:
        """Return True to enable polling."""
        return True

    @property
    def climate_settings_on(self) -> bool | None:
        """Return settingsOn based on HVACMode."""
        return self.hvac_mode == HVACMode.HEAT_COOL

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation mode."""
        return self._attr_hvac_mode

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._attr_current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._attr_target_temperature

    @property
    def front_defrost(self) -> bool:
        """Return front_defrost."""
        return self._attr_front_defrost

    @property
    def rear_defrost(self) -> bool:
        """Return rear_defrost."""
        return self._attr_rear_defrost

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode."""
        if self.front_defrost and self.rear_defrost:
            return "both_defrost"
        if self.front_defrost:
            return "front_defrost"
        if self.rear_defrost:
            return "rear_defrost"
        return "none"

    def _create_climate_settings(self) -> ClimateSettingsRequestModel:
        """Build the legacy climate-settings write body.

        NOTE (2026-07): this legacy PUT body targets the retired
        /v1/global/remote/climate-settings route and is superseded by the unified
        POST /v2/remote/climate-control request. It is kept so the control path
        type-checks; actuation correctness lands with the V2 migration PR.

        Returns:
            ClimateSettingsRequestModel configured with the specified settings
        """
        # operations() is a deprecated back-compat accessor and now returns []
        # (the new read payload has no acOperations); rebuild the defrost operation
        # from the current entity state.
        ac_operations = [
            ACOperations(
                categoryName="defrost",
                acParameters=[
                    ACParameters(enabled=self.front_defrost, name="frontDefrost"),
                    ACParameters(enabled=self.rear_defrost, name="rearDefrost"),
                ],
            )
        ]

        return ClimateSettingsRequestModel(
            settingsOn=self.climate_settings_on,
            temperature=self.target_temperature,
            temperatureUnit="C",
            acOperations=ac_operations,
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        try:
            # Update the underlying defrost attributes based on preset mode
            if preset_mode == "both_defrost":
                self._attr_front_defrost = True
                self._attr_rear_defrost = True
            elif preset_mode == "front_defrost":
                self._attr_front_defrost = True
                self._attr_rear_defrost = False
            elif preset_mode == "rear_defrost":
                self._attr_front_defrost = False
                self._attr_rear_defrost = True
            else:  # "none"
                self._attr_front_defrost = False
                self._attr_rear_defrost = False

            self.async_write_ha_state()
            self._debounce_send_climate_settings()

        except Exception:  # pylint: disable=W0718
            _LOGGER.exception("Error setting preset mode")

    async def async_update(self) -> None:
        """Poll the car for fresh climate status."""
        if not self.climate_settings_on:
            return

        try:
            # Wake the car, then refetch just climate_status (the envelope-unwrap
            # bug in vehicle.climate_status is fixed upstream, so no _api bypass).
            if await self.vehicle.refresh_climate_status():
                _LOGGER.debug("Climate status refreshed from car")
                await self.vehicle.update(only=["climate_status"])
                self._load_climate_status_from_coordinator()
                self.async_write_ha_state()

        except Exception:  # pylint: disable=W0718
            _LOGGER.exception("Error updating climate status")

        except Exception:  # pylint: disable=W0718
            _LOGGER.exception("Error updating climate settings")

    @callback
    def _debounce_send_climate_settings(self) -> None:
        """Debounce climate settings updates to avoid excessive API calls."""
        # Cancel any pending scheduled call
        if self._pending_settings_cancel is not None:
            self._pending_settings_cancel()
            self._pending_settings_cancel = None

        # Mark that settings have changed
        self._settings_changed = True

        # Schedule the actual send after a delay using HA's event loop
        self._pending_settings_cancel = async_call_later(
            self.hass, SETTINGS_DEBOUNCE_DELAY, self._delayed_send_climate_settings
        )

    async def _delayed_send_climate_settings(self, _now: Any) -> None:  # noqa: ANN401
        """Send settings after debounce delay.

        Args:
            _now: Current time (required by async_call_later, unused)
        """
        self._pending_settings_cancel = None
        if self._settings_changed:
            self._settings_changed = False
            # Fired from a timer, not a service call, so there is no UI context
            # to raise into — log instead of propagating HomeAssistantError.
            try:
                await self._send_climate_settings()
            except HomeAssistantError as err:
                _LOGGER.error("Debounced climate settings send failed: %s", err)

    async def _send_climate_settings(self) -> None:
        """Send climate settings to car.

        Raises:
            HomeAssistantError: if the car/API rejected the settings, so the
                caller can roll back optimistic state and the failure surfaces
                in the UI instead of silently leaving a stale "on" tile.
        """
        climate_settings = self._create_climate_settings()
        _LOGGER.debug("Sending climate settings to car: %s", climate_settings)
        try:
            status = await self.vehicle._api.update_climate_settings(  # noqa: SLF001
                self.vehicle.vin, climate_settings
            )
        except Exception as err:  # pylint: disable=W0718
            raise HomeAssistantError(
                f"Toyota rejected the climate settings: {err}"
            ) from err

        _LOGGER.debug("API response status: %s", status)

        # Check if the update was successful
        if not status or (hasattr(status, "status") and status.status == 0):
            raise HomeAssistantError("Toyota rejected the climate settings")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.OFF:
            await self._turn_off_climate()
        elif hvac_mode == HVACMode.HEAT_COOL:
            await self._turn_on_climate()

    async def async_set_temperature(self, **kwargs: Any) -> None:  # noqa: ANN401
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        try:
            self._attr_target_temperature = temperature
            self.async_write_ha_state()

            self._debounce_send_climate_settings()

        except Exception:  # pylint: disable=W0718
            _LOGGER.exception("Error setting climate temperature")

    async def async_turn_on(self) -> None:
        """Turn on climate control."""
        await self._turn_on_climate()

    async def async_turn_off(self) -> None:
        """Turn off climate control."""
        await self._turn_off_climate()

    async def _turn_on_climate(self) -> None:
        """Turn on the climate control."""
        # optimistically turn on the climate device
        self._attr_hvac_mode = HVACMode.HEAT_COOL
        self.async_write_ha_state()

        _LOGGER.debug("Attempting to turn on climate for %s", self.vehicle.alias)

        # Cancel any pending debounced updates
        if self._pending_settings_cancel is not None:
            self._pending_settings_cancel()
            self._pending_settings_cancel = None
        self._settings_changed = False

        try:
            # Send settings immediately when turning on
            await self._send_climate_settings()

            # Now send the engine-start command to actually turn on climate
            _LOGGER.debug("Sending engine-start command to %s", self.vehicle.alias)
            status = await self.vehicle._api.send_climate_control_command(  # noqa: SLF001
                self.vehicle.vin, ClimateControlModel(command="engine-start")
            )

            # Check if the update was successful
            if not status or (hasattr(status, "status") and status.status == 0):
                _LOGGER.debug("Failed to start engine: %s", status)
                raise HomeAssistantError(
                    "Toyota did not start the climate. Common causes: the car is "
                    "unlocked, a door/window/trunk is open, a key is inside, or "
                    "climate was already started once since the last ignition."
                )
        except Exception as err:  # pylint: disable=W0718
            # Roll back the optimistic "on" so the tile reflects reality instead
            # of falsely showing the climate running.
            self._attr_hvac_mode = HVACMode.OFF
            self.async_write_ha_state()
            if isinstance(err, HomeAssistantError):
                raise
            raise HomeAssistantError(
                f"Failed to turn on Toyota climate: {err}"
            ) from err

        _LOGGER.debug("Climate control turned on for %s", self.vehicle.alias)

    async def _turn_off_climate(self) -> None:
        """Turn off the climate control."""
        # optimistically turn off the climate device
        self._attr_hvac_mode = HVACMode.OFF
        self.async_write_ha_state()

        _LOGGER.debug("Attempting to turn off climate for %s", self.vehicle.alias)

        try:
            # Send the engine-stop command to turn off climate
            status = await self.vehicle._api.send_climate_control_command(  # noqa: SLF001
                self.vehicle.vin, ClimateControlModel(command="engine-stop")
            )
            if not status or (hasattr(status, "status") and status.status == 0):
                raise HomeAssistantError("Toyota did not confirm the climate stop")
        except Exception as err:  # pylint: disable=W0718
            # The stop was not confirmed, so the climate may still be running —
            # revert to "on" rather than falsely showing it off.
            self._attr_hvac_mode = HVACMode.HEAT_COOL
            self.async_write_ha_state()
            if isinstance(err, HomeAssistantError):
                raise
            raise HomeAssistantError(
                f"Failed to turn off Toyota climate: {err}"
            ) from err

        _LOGGER.debug("Climate control turned off for %s", self.vehicle.alias)

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        # Cancel any pending scheduled calls
        if self._pending_settings_cancel is not None:
            self._pending_settings_cancel()
            self._pending_settings_cancel = None
