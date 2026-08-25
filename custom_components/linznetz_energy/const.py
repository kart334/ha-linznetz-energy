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

# Known tariff history for the installation this integration was prepared for.
# Cost statistics intentionally include energy + base price only. Network fees,
# taxes and levies are excluded because no reliable current values are confirmed.
DEFAULT_TARIFF_HISTORY = [
    {
        "valid_from": "2024-12-24",
        "provider": "E.ON Energie Österreich",
        "name": "historischer Tarif",
        "energy_price": 0.264,
        "base_price_month": 5.40,
    },
    {
        "valid_from": "2025-10-01",
        "provider": "E.ON Energie Österreich",
        "name": "E.ON ÖkoStrom Treue",
        "energy_price": 0.152388,
        "base_price_month": 2.754,
    },
]
