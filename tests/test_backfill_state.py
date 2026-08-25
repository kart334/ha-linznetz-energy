"""Regression tests for manual backfill trigger/result semantics."""

import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "linznetz_energy"
PACKAGE_NAME = "custom_components.linznetz_energy"


def _load_module(name: str):
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_PATH)]
    sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
    sys.modules[PACKAGE_NAME] = package

    const_spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.const", PACKAGE_PATH / "const.py"
    )
    assert const_spec is not None and const_spec.loader is not None
    const_module = importlib.util.module_from_spec(const_spec)
    sys.modules[const_spec.name] = const_module
    const_spec.loader.exec_module(const_module)

    module_spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.{name}", PACKAGE_PATH / f"{name}.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module, const_module


_backfill, _const = _load_module("backfill")


def test_complete_backfill_is_success_and_consumes_trigger() -> None:
    result = _backfill.finalized_backfill_options(
        {_const.CONF_RUN_BACKFILL: True},
        requested_days=30,
        imported_days=30,
        failed_days=0,
    )

    assert result[_const.CONF_RUN_BACKFILL] is False
    assert result[_const.CONF_LAST_BACKFILL_STATUS] == _const.BACKFILL_STATUS_SUCCESS


def test_partial_backfill_is_persisted_and_consumes_trigger() -> None:
    result = _backfill.finalized_backfill_options(
        {_const.CONF_RUN_BACKFILL: True},
        requested_days=30,
        imported_days=1,
        failed_days=29,
    )

    assert result[_const.CONF_RUN_BACKFILL] is False
    assert result[_const.CONF_LAST_BACKFILL_STATUS] == _const.BACKFILL_STATUS_PARTIAL
    assert result[_const.CONF_LAST_BACKFILL_IMPORTED_DAYS] == 1
    assert result[_const.CONF_LAST_BACKFILL_FAILED_DAYS] == 29


def test_fully_failed_backfill_is_failed_and_consumes_trigger() -> None:
    result = _backfill.finalized_backfill_options(
        {_const.CONF_RUN_BACKFILL: True},
        requested_days=30,
        imported_days=0,
        failed_days=30,
    )

    assert result[_const.CONF_RUN_BACKFILL] is False
    assert result[_const.CONF_LAST_BACKFILL_STATUS] == _const.BACKFILL_STATUS_FAILED


def test_reload_after_partial_does_not_request_another_backfill() -> None:
    result = _backfill.finalized_backfill_options(
        {_const.CONF_RUN_BACKFILL: True},
        requested_days=30,
        imported_days=1,
        failed_days=29,
    )

    assert _backfill.manual_backfill_requested(result) is False


def test_normal_refresh_does_not_request_manual_backfill() -> None:
    options = {
        _const.CONF_RUN_BACKFILL: False,
        _const.CONF_LAST_BACKFILL_STATUS: _const.BACKFILL_STATUS_PARTIAL,
    }

    assert _backfill.manual_backfill_requested(options) is False


def test_legacy_30_day_trigger_is_cleared_without_backfill_request() -> None:
    legacy = {
        _const.CONF_BACKFILL_DAYS: 30,
        _const.CONF_RUN_BACKFILL: True,
    }

    migrated, cleared = _backfill.migrate_legacy_backfill_options(legacy)

    assert cleared is True
    assert migrated[_const.CONF_BACKFILL_DAYS] == 30
    assert migrated[_const.CONF_RUN_BACKFILL] is False
    assert _backfill.manual_backfill_requested(migrated) is False


def test_legacy_395_day_trigger_is_cleared_without_backfill_request() -> None:
    legacy = {
        _const.CONF_BACKFILL_DAYS: 395,
        _const.CONF_RUN_BACKFILL: True,
    }

    migrated, cleared = _backfill.migrate_legacy_backfill_options(legacy)

    assert cleared is True
    assert migrated[_const.CONF_BACKFILL_DAYS] == 395
    assert migrated[_const.CONF_RUN_BACKFILL] is False
    assert _backfill.manual_backfill_requested(migrated) is False


def test_legacy_false_trigger_keeps_normal_options_unchanged() -> None:
    legacy = {
        _const.CONF_BACKFILL_DAYS: 30,
        _const.CONF_RUN_BACKFILL: False,
        _const.CONF_LAST_BACKFILL_STATUS: _const.BACKFILL_STATUS_PARTIAL,
    }

    migrated, cleared = _backfill.migrate_legacy_backfill_options(legacy)

    assert cleared is False
    assert migrated == legacy
    assert _backfill.manual_backfill_requested(migrated) is False


def test_new_trigger_after_migration_runs_once_then_stays_consumed() -> None:
    migrated, _ = _backfill.migrate_legacy_backfill_options(
        {
            _const.CONF_BACKFILL_DAYS: 5,
            _const.CONF_RUN_BACKFILL: True,
        }
    )
    assert _backfill.manual_backfill_requested(migrated) is False

    user_requested = dict(migrated)
    user_requested[_const.CONF_RUN_BACKFILL] = True
    assert _backfill.manual_backfill_requested(user_requested) is True

    finalized = _backfill.finalized_backfill_options(
        user_requested,
        requested_days=5,
        imported_days=1,
        failed_days=4,
    )
    assert _backfill.manual_backfill_requested(finalized) is False
    assert finalized[_const.CONF_LAST_BACKFILL_STATUS] == _const.BACKFILL_STATUS_PARTIAL


def test_legacy_migration_preserves_tariff_history() -> None:
    tariff_history = '[{"valid_from":"2025-01-01","energy_price":0.2,"base_price_month":3.0}]'
    legacy = {
        _const.CONF_BACKFILL_DAYS: 395,
        _const.CONF_RUN_BACKFILL: True,
        _const.CONF_TARIFF_HISTORY: tariff_history,
    }

    migrated, _ = _backfill.migrate_legacy_backfill_options(legacy)

    assert migrated[_const.CONF_TARIFF_HISTORY] == tariff_history
    assert migrated[_const.CONF_BACKFILL_DAYS] == 395


def test_config_entry_migration_version_is_bumped_to_two() -> None:
    config_flow = (PACKAGE_PATH / "config_flow.py").read_text(encoding="utf-8")
    init_module = (PACKAGE_PATH / "__init__.py").read_text(encoding="utf-8")

    assert "VERSION = 2" in config_flow
    assert "async def async_migrate_entry" in init_module
    assert "version=2" in init_module
