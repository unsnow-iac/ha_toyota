"""Tier-B steering-heater: _build_start_request echo/override + bug regression.

Gated on a pytoyoda that ships the V2 climate models (the released PyPI 5.1.0
does not yet), AND on Home Assistant being importable — so this skips cleanly in
the current CI (PyPI-pinned) and runs once the fork's pytoyoda has V2 models.

Regression pinned here (found via a live write-then-read probe 2026-07-01): the
steering echo used ``_wire()`` on the climate-settings read value, but that read
is ALREADY an ``"on"/"off"`` string, so ``_wire("off")`` returned ``"on"`` — every
climate start silently switched the wheel heater on. The fix passes the read
through raw; the Tier-B switch overrides it.
"""

from __future__ import annotations

import pytest

# Skip unless the environment has both HA and a V2-capable pytoyoda.
pytest.importorskip("homeassistant.components.climate")
_climate_models = pytest.importorskip("pytoyoda.models.endpoints.climate")
if not hasattr(_climate_models, "V2RemoteClimateControlRequestModel"):
    pytest.skip(
        "pytoyoda lacks V2 climate models (pre-release)", allow_module_level=True
    )

from homeassistant.const import UnitOfTemperature  # noqa: E402

from custom_components.toyota.climate import ToyotaClimate  # noqa: E402


class _Heating:
    def __init__(self, steering: str | None) -> None:
        self.steering_heater = steering


class _Seats:
    def __init__(self) -> None:
        self.driver_seat = "off"
        self.passenger_seat = "off"
        self.rear_driver_seat = "off"
        self.rear_passenger_seat = "off"


class _Settings:
    def __init__(self, steering: str | None, seats: bool) -> None:
        self.heating_options = _Heating(steering)
        self.seat_options = _Seats() if seats else None
        self.temperature = None
        self.duration = None


class _Vehicle:
    def __init__(self, steering: str | None = "off", seats: bool = True) -> None:
        self.climate_settings = _Settings(steering, seats)


def _entity(steering_read: str | None = "off", override: str | None = None,
            seats: bool = True) -> ToyotaClimate:
    """A ToyotaClimate with just the fields _build_start_request touches."""
    e = object.__new__(ToyotaClimate)
    e.vehicle = _Vehicle(steering_read, seats)
    e._steering_override = override
    e._attr_front_defrost = False
    e._attr_rear_defrost = False
    e._attr_target_temperature = 20
    e._attr_temperature_unit = UnitOfTemperature.CELSIUS
    return e


def test_steering_echo_off_stays_off():
    """Regression: read "off" must echo "off", NOT "on" (the _wire bug)."""
    req = _entity(steering_read="off")._build_start_request()
    assert req.heating_options.steering_heater == "off"


def test_steering_echo_on_stays_on():
    req = _entity(steering_read="on")._build_start_request()
    assert req.heating_options.steering_heater == "on"


def test_steering_override_beats_echo():
    assert _entity("off", override="on")._build_start_request(
    ).heating_options.steering_heater == "on"
    assert _entity("on", override="off")._build_start_request(
    ).heating_options.steering_heater == "off"


def test_steering_override_none_falls_back_to_read():
    req = _entity(steering_read="on", override=None)._build_start_request()
    assert req.heating_options.steering_heater == "on"


def test_save_settings_guarded_on_full_read():
    """save_settings only when both heating+seat reads exist (never clobber)."""
    assert _entity(seats=True)._build_start_request().save_settings is True
    assert _entity(seats=False)._build_start_request().save_settings is False
