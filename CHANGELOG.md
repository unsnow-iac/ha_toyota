# Changelog

All notable changes to this fork are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

This is a downstream fork of [`pytoyoda/ha_toyota`](https://github.com/pytoyoda/ha_toyota).
Entries below are the changes this fork carries **on top of upstream `v2.3.0`**
(the `main` branch tracks upstream unchanged; all fork work lands on `testing`).

## [Unreleased] — fork divergence from `pytoyoda/ha_toyota` v2.3.0

### Added

- **Remote door lock entity** — a `lock` entity for the vehicle, with
  `assumed_state` so it stays usable through stale/lagging telemetry between
  refreshes.
- **Buzzer and Hazard-lights buttons** — `BUZZER_WARNING` and `HAZARD_ON`
  remote commands as `button` entities (the car self-stops the hazards).
  Supersedes an earlier Find-vehicle button.

### Fixed / Hardened

- **Setup-race resilience** — bound both external fetches in
  `_refresh_one_vehicle` (`STATUS_FETCH_BUDGET_S`, `SUMMARY_FETCH_BUDGET_S`) so a
  Toyota-side outage on the slow `/v1/trips` summary endpoints can no longer
  stall `async_config_entry_first_refresh` past Home Assistant's bootstrap setup
  budget. Previously a cold start during a 504 storm overran setup, got
  cancelled mid platform-forward, and left the platforms half-registered
  (`... has already been setup`), wedging the entry until a manual reload. Now
  first refresh completes (or fails cleanly with `ConfigEntryNotReady`) in
  bounded time, so Home Assistant does its own backoff retry.
- **Graceful trip-summary degradation** — on any transient summary failure
  (timeout, Toyota API/internal error, httpx/httpcore connect/read timeout, or a
  pydantic `ValidationError`) the integration holds the last-known statistics
  (or `None`) and still returns the cycle's fresh status data, instead of
  stubbing the whole vehicle as unavailable.
- **Empty-fleet crash fix** — `any_served` had a malformed double-`for`
  comprehension that raised `NameError` when Toyota returned an empty vehicle
  list (it survived only because a non-empty list short-circuited the bad
  iterable). Restored to a single clean comprehension.
- **Climate-settings HTTP 500 guards** — a `500` on the climate-settings
  endpoint during `vehicle.update` no longer aborts the whole refresh; None-op
  climate operations are guarded and the cycle continues with partial data.
- **Refresh-status 5xx resilience** — suppress 5xx on the refresh-status POST
  with a bare-`GET /status` fallback and auto-recovery that clears the
  auto-disable flag once the gateway processes a POST again; a manual
  `refresh_vehicle_status` call bypasses the hard-disabled-auto state.
- **`TrackerEntity` deprecation** — import from
  `homeassistant.components.device_tracker` directly (the
  `.config_entry` alias is removed in Home Assistant Core 2027.6).

### Synced from upstream

- Merged `pytoyoda/ha_toyota` `main` (post-2026-06-23 revival) into `testing`,
  picking up the upstream config-dir path fix and broader climate-capability
  detection — `_vehicle_has_climate_capability` now also accepts
  `econnect_climate_capable` and `remote_engine_start_stop`, so PHEVs/EVs that
  advertise climate under extended capabilities get the climate entity.
- **Did not adopt** upstream's `pytoyoda @ git+…scurkovic/pytoyoda` manifest pin
  — this fork keeps the published `pytoyoda>=5.1.0,<6.0` requirement so Hassfest
  stays green. The fork's `validate.yml` also keeps its HACS-publish-job drop
  (we deploy via `/apply-toyota`, not HACS-default).
