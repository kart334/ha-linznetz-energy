"""Coordinator for LINZ NETZ Energy."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
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
    CONF_RUN_BACKFILL,
    CONF_TARIFF_HISTORY,
    COST_STATISTIC_ID,
    COST_STATISTIC_NAME,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_TARIFF_HISTORY,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    STATISTIC_ID,
    STATISTIC_NAME,
    UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)
_VIENNA = ZoneInfo("Europe/Vienna")
_EUR = "EUR"


@dataclass(frozen=True)
class TariffPeriod:
    """One tariff period used for historical cost calculation."""

    valid_from: datetime
    energy_price: Decimal
    base_price_month: Decimal
    provider: str
    name: str


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
        self._backfill_attempted = False
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
        backfill_days = min(
            int(
                self._entry.options.get(
                    CONF_BACKFILL_DAYS,
                    self._entry.data.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
                )
            ),
            MAX_BACKFILL_DAYS,
        )
        backfill_requested = bool(
            self._entry.options.get(CONF_RUN_BACKFILL, False)
        )
        run_backfill = backfill_requested and not self._backfill_attempted

        if run_backfill:
            self._backfill_attempted = True
            start_day = yesterday - timedelta(days=max(backfill_days - 1, 0))
            _LOGGER.info("LINZ NETZ manual backfill requested: days=%s", backfill_days)
        elif last and last.get(STATISTIC_ID):
            last_start = datetime.fromtimestamp(
                last[STATISTIC_ID][0]["start"], tz=_VIENNA
            )
            start_day = max(last_start.date(), yesterday - timedelta(days=2))
        else:
            start_day = yesterday - timedelta(days=max(backfill_days - 1, 0))

        first_day_start = datetime.combine(
            start_day, datetime.min.time(), tzinfo=_VIENNA
        )
        energy_cumulative = await self._async_get_sum_before(
            STATISTIC_ID, first_day_start
        )
        cost_cumulative = await self._async_get_sum_before(
            COST_STATISTIC_ID, first_day_start
        )
        tariffs = self._load_tariffs()

        energy_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        imported_days = 0
        imported_quarters = 0
        failed_days: list[datetime.date] = []
        yesterday_total: float | None = None
        yesterday_cost: float | None = None

        day = start_day
        while day <= yesterday:
            try:
                readings = await self._client.async_fetch_quarter_readings(day)
            except LinzNetzError as err:
                _LOGGER.warning("LINZ NETZ %s konnte nicht gelesen werden: %s", day, err)
                failed_days.append(day)
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
                failed_days.append(day)
                day += timedelta(days=1)
                continue

            day_cost_decimal = Decimal("0")
            day_cost_complete = True
            tariff_labels: set[str] = set()

            _LOGGER.info(
                "LINZ NETZ statistics day=%s hours=%s previous_sum=%.6f",
                day,
                len(hourly),
                energy_cumulative,
            )

            for start, value in sorted(hourly.items()):
                energy_cumulative += value
                energy_stats.append(
                    StatisticData(start=start, state=value, sum=energy_cumulative)
                )

                tariff = self._tariff_for(start, tariffs)
                if tariff is None:
                    day_cost_complete = False
                    continue

                tariff_labels.add(
                    f"{tariff.valid_from.date()} {tariff.provider} {tariff.name}"
                )
                hour_cost = self._hour_cost(start, value, tariff)
                day_cost_decimal += hour_cost
                cost_cumulative += float(hour_cost)
                cost_stats.append(
                    StatisticData(
                        start=start,
                        state=float(hour_cost),
                        sum=cost_cumulative,
                    )
                )

            imported_quarters += len(readings)
            imported_days += 1

            day_total = quarter_total
            if day == yesterday:
                yesterday_total = day_total
                if day_cost_complete:
                    yesterday_cost = float(day_cost_decimal)

            if tariff_labels:
                _LOGGER.info(
                    "LINZ NETZ tariffs day=%s periods=%s",
                    day,
                    sorted(tariff_labels),
                )
            if not day_cost_complete:
                _LOGGER.warning(
                    "LINZ NETZ Kosten fuer %s unvollstaendig: keine bestaetigte "
                    "Tarifperiode fuer mindestens eine Stunde",
                    day,
                )

            day += timedelta(days=1)

        if energy_stats:
            async_add_external_statistics(
                self.hass,
                self._energy_metadata(),
                energy_stats,
            )

        if cost_stats:
            async_add_external_statistics(
                self.hass,
                self._cost_metadata(),
                cost_stats,
            )

        _LOGGER.info(
            "LINZ NETZ import complete: days=%s quarters=%s energy_points=%s "
            "cost_points=%s failed_days=%s",
            imported_days,
            imported_quarters,
            len(energy_stats),
            len(cost_stats),
            len(failed_days),
        )

        if run_backfill:
            if failed_days:
                _LOGGER.warning(
                    "LINZ NETZ manual backfill incomplete: requested_days=%s "
                    "imported_days=%s failed_days=%s; request flag remains set",
                    backfill_days,
                    imported_days,
                    len(failed_days),
                )
            else:
                self.hass.async_create_task(self._async_clear_backfill_request())

        return {
            "last_sync": datetime.now(_VIENNA),
            "yesterday_kwh": yesterday_total,
            "yesterday_cost_eur": yesterday_cost,
            "new_hourly_statistics": len(energy_stats),
            "new_cost_statistics": len(cost_stats),
            "days_checked": imported_days,
            "failed_days": len(failed_days),
            "backfill_complete": not run_backfill or not failed_days,
        }

    async def _async_clear_backfill_request(self) -> None:
        """Clear the one-shot manual backfill flag after a complete run."""
        options = dict(self._entry.options)
        if not options.get(CONF_RUN_BACKFILL):
            return
        options[CONF_RUN_BACKFILL] = False
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    @staticmethod
    def _energy_metadata() -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=STATISTIC_NAME,
            source=DOMAIN,
            statistic_id=STATISTIC_ID,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )

    @staticmethod
    def _cost_metadata() -> StatisticMetaData:
        return StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=COST_STATISTIC_NAME,
            source=DOMAIN,
            statistic_id=COST_STATISTIC_ID,
            unit_class=None,
            unit_of_measurement=_EUR,
        )

    async def _async_get_sum_before(
        self, statistic_id: str, start: datetime
    ) -> float:
        """Return the cumulative statistic sum immediately before ``start``."""
        first_hour_end = start + timedelta(hours=1)
        current = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            first_hour_end,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        rows = current.get(statistic_id, [])
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
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        previous_rows = previous.get(statistic_id, [])
        if not previous_rows:
            return 0.0
        return float(previous_rows[-1].get("sum") or 0.0)

    def _load_tariffs(self) -> list[TariffPeriod]:
        """Load validated tariff history from options or built-in confirmed data."""
        raw = self._entry.options.get(CONF_TARIFF_HISTORY)
        source = DEFAULT_TARIFF_HISTORY
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    source = parsed
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "LINZ NETZ Tarifhistorie ungueltig; bestaetigte Standardhistorie wird verwendet"
                )

        periods: list[TariffPeriod] = []
        for item in source:
            try:
                valid_from = datetime.strptime(
                    str(item["valid_from"]), "%Y-%m-%d"
                ).replace(tzinfo=_VIENNA)
                periods.append(
                    TariffPeriod(
                        valid_from=valid_from,
                        energy_price=Decimal(str(item["energy_price"])),
                        base_price_month=Decimal(str(item["base_price_month"])),
                        provider=str(item.get("provider", "")),
                        name=str(item.get("name", "")),
                    )
                )
            except (KeyError, TypeError, ValueError, ArithmeticError):
                _LOGGER.warning(
                    "LINZ NETZ Tarifperiode ignoriert: unvollstaendige oder ungueltige Felder"
                )
        return sorted(periods, key=lambda period: period.valid_from)

    @staticmethod
    def _tariff_for(
        start: datetime, tariffs: list[TariffPeriod]
    ) -> TariffPeriod | None:
        applicable = [period for period in tariffs if period.valid_from <= start]
        return applicable[-1] if applicable else None

    @staticmethod
    def _hours_in_month(start: datetime) -> Decimal:
        """Return actual local-clock hours in the calendar month, including DST."""
        month_start = datetime(start.year, start.month, 1, tzinfo=_VIENNA)
        if start.month == 12:
            next_month = datetime(start.year + 1, 1, 1, tzinfo=_VIENNA)
        else:
            next_month = datetime(start.year, start.month + 1, 1, tzinfo=_VIENNA)
        seconds = (
            next_month.astimezone(timezone.utc)
            - month_start.astimezone(timezone.utc)
        ).total_seconds()
        return Decimal(str(seconds / 3600))

    @classmethod
    def _hour_cost(
        cls, start: datetime, energy_kwh: float, tariff: TariffPeriod
    ) -> Decimal:
        """Calculate energy + prorated monthly base price for one hour."""
        energy = Decimal(str(energy_kwh)) * tariff.energy_price
        base = tariff.base_price_month / cls._hours_in_month(start)
        return energy + base

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
