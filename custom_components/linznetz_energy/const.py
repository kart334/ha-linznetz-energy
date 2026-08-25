"""Constants for LINZ NETZ Energy."""

DOMAIN = "linznetz_energy"
CONF_BACKFILL_DAYS = "backfill_days"

DEFAULT_BACKFILL_DAYS = 30
UPDATE_INTERVAL_HOURS = 12

PORTAL_URL = (
    "https://services.linznetz.at/verbrauchsdateninformation/consumption.jsf"
)

STATISTIC_ID = f"{DOMAIN}:energy_consumption"
STATISTIC_NAME = "LINZ NETZ Stromverbrauch"
