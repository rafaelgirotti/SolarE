"""Growatt inverter polling - solar-aware scheduling reads live power output through this.

Reads `tlx_data`'s per-5-minute `invPacData` series rather than `tlx_system_status`'s live `pac`
field - the former's watt scale is confirmed against a known peak-power reading, the latter's
units were never verified. Confirm your own device is `tlx`-family (via growattServer's
`device_list`) before relying on this - other Growatt device families expose different data
methods.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import growattServer

    _GROWATT_AVAILABLE = True
except ImportError:
    _GROWATT_AVAILABLE = False


@dataclass
class GenerationSummary:
    current_power_w: float
    today_kwh: float
    month_kwh: float
    total_kwh: float
    nominal_power_w: float


@dataclass
class GrowattCredentials:
    server_url: str
    username: str
    password: str
    device_sn: str

    @classmethod
    def from_file(cls, path: Path | str) -> "GrowattCredentials":
        with Path(path).open() as f:
            data = json.load(f)
        return cls(
            server_url=data["server_url"],
            username=data["username"],
            password=data["password"],
            device_sn=data["device_sn"],
        )


class GrowattClient:
    def __init__(self, credentials: GrowattCredentials):
        if not _GROWATT_AVAILABLE:
            raise RuntimeError(
                "growattServer isn't installed - run `uv sync --extra solar` to enable solar monitoring"
            )
        self._credentials = credentials
        # add_random_user_id works around a known growattServer/API quirk where repeated logins
        # from the same synthesized user id can get rejected.
        self._api = growattServer.GrowattApi(add_random_user_id=True)
        self._api.server_url = credentials.server_url
        self._logged_in = False
        self._plant_id: str | None = None

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        response = self._api.login(self._credentials.username, self._credentials.password)
        if not response.get("success"):
            raise RuntimeError(f"Growatt login failed: {response.get('msg')}")
        self._logged_in = True

    def _get_plant_id(self) -> str:
        """Assumes a single-plant account, matching this project's `credentials.json` schema
        (one `device_sn`, no `plant_id` field) - not built for multi-plant accounts."""
        if self._plant_id is None:
            self._ensure_login()
            plants = self._api.plant_list_two()
            if not plants:
                raise RuntimeError("Growatt account has no plants")
            self._plant_id = str(plants[0]["id"])
        return self._plant_id

    def get_power_series(
        self, date: datetime.datetime | None = None
    ) -> dict[datetime.datetime, float]:
        """Return one day's 5-minute power series (watts), keyed by timestamp."""
        self._ensure_login()
        date = date or datetime.datetime.now()
        data = self._api.tlx_data(self._credentials.device_sn, date=date)
        series = data.get("invPacData", {})
        return {
            datetime.datetime.strptime(key, "%Y-%m-%d %H:%M"): float(value)
            for key, value in series.items()
        }

    def get_latest_power_w(
        self, min_age: datetime.timedelta = datetime.timedelta(minutes=5)
    ) -> tuple[float, datetime.datetime]:
        """Return the most recent power reading at least `min_age` old.

        Growatt appears to pre-populate the current in-progress 5-minute bucket with a 0-valued
        placeholder before backfilling the real reading - buckets younger than `min_age` are
        skipped rather than trusted at face value.
        """
        now = datetime.datetime.now()
        cutoff = now - min_age
        eligible = {ts: w for ts, w in self.get_power_series(now).items() if ts <= cutoff}
        if not eligible:
            raise RuntimeError(f"no power readings at least {min_age} old yet today")
        latest_ts = max(eligible)
        return eligible[latest_ts], latest_ts

    def is_producing(self, threshold_w: float) -> bool:
        power_w, _ = self.get_latest_power_w()
        return power_w >= threshold_w

    def get_generation_summary(self) -> GenerationSummary:
        """Today's/this month's/lifetime generation plus current power, via `plant_energy_data`.

        Confirmed working (see docs/growatt-api.md) - unlike household consumption/import/battery
        data, which reads 0 without a smart meter/CT clamp attached to the inverter, generation
        totals are populated regardless. `yearValue` is a separate field on the same response but
        reads "0.0" on the test account (real gap or account-specific, unconfirmed either way) -
        deliberately not exposed here since there's nothing real to report for it yet.
        """
        self._ensure_login()
        data = self._api.plant_energy_data(self._get_plant_id())
        return GenerationSummary(
            current_power_w=float(data["powerValue"]),
            today_kwh=float(data["todayValue"]),
            month_kwh=float(data["monthValue"]),
            total_kwh=float(data["totalValue"]),
            nominal_power_w=_parse_nominal_power_w(data["nominalPowerStr"]),
        )


def _parse_nominal_power_w(nominal_power_str: str) -> float:
    """"7.5kWp" -> 7500.0 - confirmed format from a live account; a bare "Wp" (no k prefix) is
    handled too on the assumption a small enough system would report that way, though unverified."""
    match = re.match(r"([\d.]+)\s*(k?)Wp", nominal_power_str)
    if not match:
        raise ValueError(f"Unrecognized nominalPowerStr format: {nominal_power_str!r}")
    value, kilo = match.groups()
    return float(value) * (1000.0 if kilo else 1.0)
