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
