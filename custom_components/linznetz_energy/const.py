"""Constants for LINZ NETZ Energy."""

DOMAIN = "linznetz_energy"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_RUN_BACKFILL = "run_backfill"
CONF_TARIFF_HISTORY = "tariff_history"

DEFAULT_BACKFILL_DAYS = 30
MAX_BACKFILL_DAYS = 395
UPDATE_INTERVAL_HOURS = 12

PORTAL_URL = (
    "https://services.linznetz.at/verbrauchsdateninformation/consumption.jsf"
)

STATISTIC_ID = f"{DOMAIN}:energy_consumption"
STATISTIC_NAME = "LINZ NETZ Stromverbrauch"
COST_STATISTIC_ID = f"{DOMAIN}:energy_cost"
COST_STATISTIC_NAME = "LINZ NETZ Energiekosten"

# Tariffs are installation-specific and must be configured by the user.
# The public integration intentionally ships no provider/contract-specific
# defaults. If no tariff history is configured, consumption import continues
# to work while cost statistics remain unavailable.
DEFAULT_TARIFF_HISTORY: list[dict[str, object]] = []
