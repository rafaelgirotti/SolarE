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
from dataclasses import dataclass
from pathlib import Path

import growattServer


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
        self._credentials = credentials
        # add_random_user_id works around a known growattServer/API quirk where repeated logins
        # from the same synthesized user id can get rejected.
        self._api = growattServer.GrowattApi(add_random_user_id=True)
        self._api.server_url = credentials.server_url
        self._logged_in = False

    def _ensure_login(self) -> None:
        if self._logged_in:
            return
        response = self._api.login(self._credentials.username, self._credentials.password)
        if not response.get("success"):
            raise RuntimeError(f"Growatt login failed: {response.get('msg')}")
        self._logged_in = True

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
