"""Coordinator for LINZ NETZ Energy."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import EnergyConverter

from .api import LinzNetzAuthError, LinzNetzClient, LinzNetzError
from .const import (
    CONF_BACKFILL_DAYS,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    STATISTIC_ID,
    STATISTIC_NAME,
    UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)
_VIENNA = ZoneInfo("Europe/Vienna")


class LinzNetzCoordinator(DataUpdateCoordinator[dict[str, object]]):
    """Fetch delayed meter data and add it as external HA statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LinzNetzClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="LINZ NETZ Energy",
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self._client = client
        self._entry = entry
        self.async_add_listener(self._dummy_listener)

    @callback
    def _dummy_listener(self) -> None:
        """Keep periodic refresh active even without sensor listeners."""

    async def _async_update_data(self) -> dict[str, object]:
        try:
            await self._client.async_login()
        except LinzNetzAuthError as err:
            raise ConfigEntryAuthFailed from err
        except LinzNetzError as err:
            raise UpdateFailed(f"LINZ NETZ Login fehlgeschlagen: {err}") from err

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            STATISTIC_ID,
            True,
            set(),
        )

        yesterday = datetime.now(_VIENNA).date() - timedelta(days=1)
        backfill_days = int(
            self._entry.options.get(
                CONF_BACKFILL_DAYS,
                self._entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
            )
        )

        if last and last.get(STATISTIC_ID):
            last_start = datetime.fromtimestamp(
                last[STATISTIC_ID][0]["start"], tz=_VIENNA
            )
            start_day = max(
                last_start.date(),
                yesterday - timedelta(days=2),
            )
            cumulative = float(last[STATISTIC_ID][0].get("sum") or 0.0)
            last_timestamp = float(last[STATISTIC_ID][0]["start"])
        else:
            start_day = yesterday - timedelta(days=max(backfill_days - 1, 0))
            cumulative = 0.0
            last_timestamp = -1.0

        stats: list[StatisticData] = []
        imported_days = 0
        yesterday_total: float | None = None

        day = start_day
        while day <= yesterday:
            try:
                readings = await self._client.async_fetch_quarter_readings(day)
            except LinzNetzError as err:
                _LOGGER.warning("LINZ NETZ %s konnte nicht gelesen werden: %s", day, err)
                day += timedelta(days=1)
                continue

            hourly = self._aggregate_hourly(readings)
            day_total = sum(hourly.values())
            if day == yesterday:
                yesterday_total = day_total

            for start, value in sorted(hourly.items()):
                ts = start.timestamp()
                if ts <= last_timestamp:
                    continue
                cumulative += value
                stats.append(StatisticData(start=start, state=value, sum=cumulative))
                last_timestamp = ts

            imported_days += 1
            day += timedelta(days=1)

        if stats:
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=STATISTIC_NAME,
                source=DOMAIN,
                statistic_id=STATISTIC_ID,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)

        return {
            "last_sync": datetime.now(_VIENNA),
            "yesterday_kwh": yesterday_total,
            "new_hourly_statistics": len(stats),
            "days_checked": imported_days,
        }

    @staticmethod
    def _aggregate_hourly(readings) -> dict[datetime, float]:
        """Aggregate portal-order quarter-hours to timezone-aware local hours.

        On the autumn DST transition LINZ NETZ can return the 02:xx hour twice.
        The second sequence is assigned ``fold=1``.
        """
        values: dict[datetime, float] = defaultdict(float)
        fold = 0
        previous_local: datetime | None = None

        for reading in readings:
            if previous_local is not None and reading.start_local < previous_local:
                fold = 1

            naive_hour = reading.start_local.replace(
                minute=0, second=0, microsecond=0
            )
            aware_hour = naive_hour.replace(tzinfo=_VIENNA, fold=fold)
            values[aware_hour] += reading.kwh
            previous_local = reading.start_local

        return dict(values)
