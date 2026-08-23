# Growatt API reference

Growatt has no public API a consumer account can use directly - only a partner/integrator API
requiring a token Growatt issues to businesses. Every community library, including the one this
project depends on, works by replaying the same session-based login the official ShinePhone
mobile app uses, reverse-engineered by capturing that app's own traffic.

**Source**: [`growattServer`](https://pypi.org/project/growattServer/)
([indykoning/PyPi_GrowattServer](https://github.com/indykoning/PyPi_GrowattServer) on GitHub -
its own user-agent string identifies this as its origin). Its
[`docs/shinephone.md`](https://github.com/indykoning/PyPi_GrowattServer/blob/master/docs/shinephone.md)
describes the reverse-engineering method: capturing the ShinePhone Android app's traffic with a
TLS-intercepting proxy (e.g. NetCapture) while operating the app, then replaying the same
endpoints/parameters directly. Nothing in this project talks to Growatt outside that library.

## Method inventory (from the installed library's own source)

Everything below is read directly from `growattServer`'s `base_api.py` - method signatures and
docstrings are the library's own; response field names are filled in only where the library
documents them, or where this project has independently verified them against a live account
(see `solare/solar/client.py`).

### tlx-family (this project's device type - confirm your own via `device_list`)

| Method | What it returns |
| --- | --- |
| `tlx_data(tlx_id, date)` | Per-5-minute series for one day. **Field this project uses**: `invPacData` (inverter output, watts) - confirmed accurate against an independently-reported peak-power reading. |
| `tlx_system_status(plant_id, tlx_id)` | Live system status. Confirmed fields include `pac` (live output power - `"unit": "kW"` alongside it checked against a live daytime reading and it's a plausible kW-scale value, resolving the earlier day/night-only-tested uncertainty), plus live AC voltage/frequency (`vac1`, `fAc`), per-MPPT PV voltage/current (`vPv1`/`iPv1`, `vPv2`/`iPv2`, ...), and `bMerterConnectFlag` (`-1` on an account with no smart meter/CT clamp attached - see "Household consumption" below). Deep diagnostic detail beyond what a scheduling gate needs, but a real candidate for an inverter-detail view later. |
| `tlx_energy_overview(plant_id, tlx_id)` | Confirmed fields: `epvToday`/`epvTotal` (generation, today/lifetime), `eselfToday`/`eselfTotal`, `elocalLoadToday`/`elocalLoadTotal`, `gridPowerToday`/`gridPowerTotal`, `echargetoday`/`echargetotal`, `edischargeToday`/`edischargeTotal` (battery-related) - all read as `"0"` on an account with no battery/meter attached; only the `epv*` (photovoltaic) fields carried real values. |
| `tlx_energy_prod_cons(plant_id, tlx_id, timespan, date)` | Confirmed structure: a `chartData` series (5-minute buckets, each with `ppv`, `sysOut`, `userLoad`, `pacToUser`, `pacToGrid`, `pex`, `chargePower`, `outP`, `pself`) plus day totals (`eCharge`, `eAcCharge`, `elocalLoad`, `photovoltaic`, ...) and a `keyNames` label list (`"Photovoltaic Output"`, `"Load Consumption"`, `"Imported From Grid"`, `"From Battery"`) confirming the endpoint is designed to report consumption, not just generation. **On an account with no smart meter/CT clamp**: every field except `ppv` read `0` across an entire day, including daylight hours where `ppv` had real non-zero values - see "Household consumption" below. |
| `tlx_detail(tlx_id)` | Detailed inverter data - per-MPPT electrical detail, largely overlapping `tlx_system_status`. |
| `tlx_params(tlx_id)` | Inverter configuration parameters, not live telemetry. |
| `tlx_all_settings` / `tlx_enabled_settings` | Inverter settings. |
| `tlx_battery_info(serial_num)` / `tlx_battery_info_detailed(plant_id, serial_num)` | Battery data - only meaningful if a battery is attached; this project's test account has none. |

### Plant-level

| Method | What it returns |
| --- | --- |
| `plant_energy_data(plant_id)` | **Confirmed working, this is the source for generation totals.** Fields: `todayValue`/`todayStr` (today, kWh), `monthValue`/`monthStr` (this month), `totalValue`/`totalStr` (lifetime), `powerValue`/`powerValueStr` (current live power), plus plant metadata (`nominalPowerStr` - rated capacity) and even a `weatherMap` (current conditions at the plant's location). `yearValue` read `"0.0"` on the test account - unclear if that's a real gap or account-specific. |
| `plant_info(plant_id)` / `device_list(plant_id)` | Plant/device metadata - already used to confirm this account's device is `tlx`-family. `device_list`'s entries (and `plant_info`'s `invList`) also carry `bMerterConnectFlag`. |
| `dashboard_data(plant_id, timespan, date)` | **Does not return data for `tlx` systems at all** (the library's own docstring says so explicitly - use `plant_energy_data` instead). Documented in detail for `Mix` (battery-hybrid) systems only: `ppv` (solar generation), `sysOut` (load consumption), `pacToUser` (power from battery), `etouser` (grid import), `ratio1`-`ratio6` (self-consumption/export percentages), and more - not applicable to this project's device type, listed here only so it's not mistakenly reached for later. |
| `plant_detail(plant_id, timespan, date)` | Period-specific energy data, plant-level. |

`timespan` above is `growattServer.base_api.Timespan`: `hour` (default), `day`, or `month`.

## Household consumption: confirmed NOT available without a meter/CT clamp

`bMerterConnectFlag` appears on `device_list`, `tlx_system_status`, and `plant_info`'s device
entries. On a test account with a plain grid-tie `tlx` inverter (no battery, no smart meter/CT
clamp), this reads `-1` on every one of those, and every consumption/load/import/battery field
across `tlx_energy_overview` and `tlx_energy_prod_cons` reads `0` all day - including through
daylight hours where the same responses' own `ppv`/generation fields carried real non-zero
values. That's a real (not just plausible) explanation, not a guess: the API has the *shape* for
consumption data on every relevant endpoint, but this account's hardware doesn't feed it anything
to report. `bMerterConnectFlag != -1` is the field to check before trusting any consumption/import
value from this API on a different account.

## What this means for solar-aware scheduling

Confirmed and in use today (`solare/solar/client.py`): live-ish generation wattage via
`tlx_data`'s `invPacData`, up to ~5 minutes stale, used for the on/off threshold check.

Confirmed available, not yet wired in: today's/monthly/lifetime generation totals and current
power via `plant_energy_data`.

Confirmed NOT available on a meter-less `tlx` account: live household consumption, grid
import/export, and battery flow - the API has fields for all of these, but they report `0`
without a smart meter/CT clamp attached to the inverter.
