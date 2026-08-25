"""Coordinator for LINZ NETZ Energy."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import logging
import math
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
    statistics_during_period,
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
            {"state", "sum"},
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
        else:
            start_day = yesterday - timedelta(days=max(backfill_days - 1, 0))

        first_day_start = datetime.combine(start_day, datetime.min.time(), tzinfo=_VIENNA)
        cumulative = await self._async_get_sum_before(first_day_start)

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
            hourly_total = sum(hourly.values())
            quarter_total = sum(reading.kwh for reading in readings)
            if not math.isclose(
                hourly_total,
                quarter_total,
                rel_tol=1e-9,
                abs_tol=1e-6,
            ):
                _LOGGER.warning(
                    "LINZ NETZ Statistik-Plausibilitaet fehlgeschlagen: "
                    "day=%s hours=%s hourly_total=%.6f quarter_total=%.6f",
                    day,
                    len(hourly),
                    hourly_total,
                    quarter_total,
                )
                day += timedelta(days=1)
                continue

            day_total = quarter_total
            if day == yesterday:
                yesterday_total = day_total

            _LOGGER.info(
                "LINZ NETZ statistics day=%s hours=%s previous_sum=%.6f",
                day,
                len(hourly),
                cumulative,
            )

            # Always write the complete day again. Home Assistant's external
            # statistics importer updates an existing row when statistic_id and
            # start timestamp already exist, so a previous partial import is
            # corrected instead of being skipped.
            for start, value in sorted(hourly.items()):
                cumulative += value
                stats.append(StatisticData(start=start, state=value, sum=cumulative))

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

    async def _async_get_sum_before(self, start: datetime) -> float:
        """Return the cumulative statistic sum immediately before ``start``.

        Prefer deriving the base from an existing row at the start of the day.
        That makes re-imports deterministic even when that day was previously
        only partially imported. If the first row is missing, fall back to the
        latest historical sum before the day.
        """
        first_hour_end = start + timedelta(hours=1)
        current = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            first_hour_end,
            {STATISTIC_ID},
            "hour",
            None,
            {"state", "sum"},
        )
        rows = current.get(STATISTIC_ID, [])
        if rows:
            first = rows[0]
            if float(first.get("start", -1.0)) == start.timestamp():
                first_sum = float(first.get("sum") or 0.0)
                first_state = float(first.get("state") or 0.0)
                return first_sum - first_state

        history_start = datetime.fromtimestamp(0, tz=_VIENNA)
        previous = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            history_start,
            start,
            {STATISTIC_ID},
            "hour",
            None,
            {"sum"},
        )
        previous_rows = previous.get(STATISTIC_ID, [])
        if not previous_rows:
            return 0.0
        return float(previous_rows[-1].get("sum") or 0.0)

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
