"""Regression tests for the browser-contract portal fixes."""

from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
import types

from bs4 import BeautifulSoup
import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = ROOT / "custom_components" / "linznetz_energy"
PACKAGE_NAME = "custom_components.linznetz_energy"


def _load(name: str):
    package = sys.modules.get(PACKAGE_NAME)
    if package is None:
        package = types.ModuleType(PACKAGE_NAME)
        package.__path__ = [str(PACKAGE_PATH)]
        sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
        sys.modules[PACKAGE_NAME] = package
    if f"{PACKAGE_NAME}.const" not in sys.modules:
        const_spec = importlib.util.spec_from_file_location(
            f"{PACKAGE_NAME}.const", PACKAGE_PATH / "const.py"
        )
        assert const_spec is not None and const_spec.loader is not None
        const_module = importlib.util.module_from_spec(const_spec)
        sys.modules[const_spec.name] = const_module
        const_spec.loader.exec_module(const_module)
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.{name}", PACKAGE_PATH / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


api = _load("api")
_load("diagnostics")
_load("date_contract_diagnostics")
_load("inline_date_handler_diagnostics")
fix = _load("browser_contract_client")


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    async def text(self) -> str:
        return self._body


class _FakeSession:
    def __init__(self, body: str | list[str]) -> None:
        self.body = body
        self.posts: list[dict[str, object]] = []

    async def post(self, url: str, **kwargs):
        index = len(self.posts)
        self.posts.append({"url": url, **kwargs})
        body = self.body[index] if isinstance(self.body, list) else self.body
        return _FakeResponse(body)


def _form() -> BeautifulSoup:
    return BeautifulSoup(
        """
        <form id="myForm1">
          <input type="hidden" name="jakarta.faces.ViewState" value="state-1" />
          <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion"
                 type="text" value="24.08.2026"
                 onchange="initCalendar();PrimeFaces.ab({p:'myForm1:calendarFromRegion',u:'myForm1',e:'change'});" />
          <span id="myForm1:panel_calendarToRegion">
            <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion"
                   type="text" value="24.08.2026"
                   onchange="assignToDate([{name:'assignToDate',value:this.value}]);" />
          </span>
          <input name="myForm1:q:selectedClass" value="ConsumQuarter" checked="checked" type="radio" />
          <input name="myForm1:k:selectedClass" value="KWH" checked="checked" type="radio" />
          <span id="myForm1:list"></span>
          <input id="myForm1:btnIdA1" type="button" value="Anzeigen" />
        </form>
        <form id="myform">
          <input type="hidden" name="myform" value="myform" />
          <input type="hidden" name="jakarta.faces.ViewState" value="to-state-1" />
        </form>
        <script>
          function assignToDate() {
            PrimeFaces.ab({s:'myform:j_idt1320',f:'myform',
              u:'@([id$=panel_calendarToRegion])',pa:arguments[0]});
          }
        </script>
        """,
        "html.parser",
    )


@pytest.mark.asyncio
async def test_unchanged_from_onchange_then_confirmed_to_ajax() -> None:
    from_partial = """
    <partial-response><changes>
      <update id="myForm1"><![CDATA[
        <form id="myForm1">
          <input type="hidden" name="jakarta.faces.ViewState" value="state-2" />
          <input id="myForm1:calendarFromRegion" name="myForm1:calendarFromRegion" type="text" value="21.08.2026" onchange="PrimeFaces.ab({p:'myForm1:calendarFromRegion',u:'myForm1',e:'change'});" />
          <span id="myForm1:panel_calendarToRegion">
            <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion"
                   type="text" value="24.08.2026"
                   onchange="assignToDate([{name:'assignToDate',value:this.value}]);" />
          </span>
          <input name="myForm1:q:selectedClass" value="ConsumQuarter" checked="checked" type="radio" />
          <input name="myForm1:k:selectedClass" value="KWH" checked="checked" type="radio" />
          <span id="myForm1:list"></span>
          <input id="myForm1:btnIdA1" type="button" value="Anzeigen" />
        </form>
      ]]></update>
      <update id="jakarta.faces.ViewState"><![CDATA[state-2]]></update>
    </changes></partial-response>
    """
    to_partial = """
    <partial-response><changes>
      <update id="myForm1:panel_calendarToRegion"><![CDATA[
        <span id="myForm1:panel_calendarToRegion">
          <input id="myForm1:calendarToRegion" name="myForm1:calendarToRegion"
                 type="text" value="21.08.2026"
                 onchange="assignToDate([{name:'assignToDate',value:this.value}]);" />
        </span>
      ]]></update>
      <update id="j_id1:jakarta.faces.ViewState:0"><![CDATA[]]></update>
    </changes></partial-response>
    """
    session = _FakeSession([from_partial, to_partial])
    client = fix.BrowserContractLinzNetzClient(session, "u", "p")
    soup = _form()
    form = soup.find("form")
    assert form is not None
    date_from = form.find(id="myForm1:calendarFromRegion")
    date_to = form.find(id="myForm1:calendarToRegion")
    assert date_from is not None and date_to is not None

    view_state, updated_form, complete = await client._async_select_day(
        behavior_soups=[soup],
        form=form,
        form_id="myForm1",
        date_from=date_from,
        date_to=date_to,
        quarter=api.ChoiceField("myForm1:q:selectedClass", "ConsumQuarter"),
        kwh=api.ChoiceField("myForm1:k:selectedClass", "KWH"),
        requested_day=date(2026, 8, 21),
        view_state_name="jakarta.faces.ViewState",
        view_state_value="state-1",
    )

    assert complete is True
    assert view_state == "state-2"
    assert len(session.posts) == 2
    from_payload = session.posts[0]["data"]
    assert from_payload["jakarta.faces.partial.execute"] == "myForm1:calendarFromRegion"
    assert from_payload["jakarta.faces.partial.render"] == "myForm1"
    assert from_payload["jakarta.faces.behavior.event"] == "change"
    assert "jakarta.faces.source" not in from_payload
    assert "jakarta.faces.partial.event" not in from_payload
    assert from_payload["jakarta.faces.ViewState"] == "state-1"
    assert from_payload["myForm1:calendarToRegion"] == "21.08.2026"
    assert from_payload["myForm1:calendarFromRegion"] == "21.08.2026"

    to_payload = session.posts[1]["data"]
    assert to_payload["jakarta.faces.source"] == "myform:j_idt1320"
    assert (
        to_payload["jakarta.faces.partial.render"]
        == "@([id$=panel_calendarToRegion])"
    )
    assert to_payload["assignToDate"] == "21.08.2026"
    assert to_payload["myform"] == "myform"
    assert to_payload["jakarta.faces.ViewState"] == "state-2"
    assert "myForm1:calendarToRegion" not in to_payload
    assert "myForm1:calendarFromRegion" not in to_payload
    assert "myForm1:q:selectedClass" not in to_payload
    assert "myForm1:k:selectedClass" not in to_payload
    assert "jakarta.faces.partial.execute" not in to_payload
    assert "jakarta.faces.behavior.event" not in to_payload
    assert "jakarta.faces.partial.event" not in to_payload

    assert updated_form.find(id="myForm1:calendarFromRegion").get("value") == "21.08.2026"
    assert updated_form.find(id="myForm1:calendarToRegion").get("value") == "21.08.2026"



@pytest.mark.asyncio
async def test_to_ajax_fails_closed_before_request_when_value_role_changes() -> None:
    session = _FakeSession("<partial-response />")
    client = fix.BrowserContractLinzNetzClient(session, "u", "p")
    soup = _form()
    form = soup.find("form")
    assert form is not None
    date_from = form.find(id="myForm1:calendarFromRegion")
    date_to = form.find(id="myForm1:calendarToRegion")
    assert date_from is not None and date_to is not None
    date_to["onchange"] = str(date_to["onchange"]).replace(
        "value:this.value", "value:this"
    )

    with pytest.raises(api.LinzNetzParseError, match="bestätigten PrimeFaces-Contract"):
        await client._async_select_day(
            behavior_soups=[soup],
            form=form,
            form_id="myForm1",
            date_from=date_from,
            date_to=date_to,
            quarter=api.ChoiceField("myForm1:q:selectedClass", "ConsumQuarter"),
            kwh=api.ChoiceField("myForm1:k:selectedClass", "KWH"),
            requested_day=date(2026, 8, 21),
            view_state_name="jakarta.faces.ViewState",
            view_state_value="state-1",
        )

    assert session.posts == []


def test_display_payload_matches_confirmed_button_contract() -> None:
    soup = _form()
    form = soup.find("form")
    assert form is not None
    date_from = form.find(id="myForm1:calendarFromRegion")
    date_to = form.find(id="myForm1:calendarToRegion")
    assert date_from is not None and date_to is not None

    payload = fix.BrowserContractLinzNetzClient._build_display_payload(
        form,
        "myForm1",
        "myForm1:btnIdA1",
        date_from,
        date_to,
        api.ChoiceField("myForm1:q:selectedClass", "ConsumQuarter"),
        api.ChoiceField("myForm1:k:selectedClass", "KWH"),
        "24.08.2026",
        "jakarta.faces.ViewState",
        "state-2",
    )

    assert payload["jakarta.faces.source"] == "myForm1:btnIdA1"
    assert payload["jakarta.faces.partial.execute"] == "myForm1:btnIdA1"
    assert payload["jakarta.faces.partial.execute"] != "@form"
    assert payload["jakarta.faces.partial.render"] == "myForm1:list"
    assert payload["jakarta.faces.behavior.event"] == "action"
    assert "jakarta.faces.partial.event" not in payload


def test_kwh_remains_plain_form_value_without_own_ajax() -> None:
    soup = _form()
    form = soup.find("form")
    assert form is not None
    payload = fix.BrowserContractLinzNetzClient._collect_form_payload(form)
    assert payload["myForm1:k:selectedClass"] == "KWH"
    assert not any("kwh" in key.lower() and "faces" in key.lower() for key in payload)


def test_known_valid_page_still_parses_96_quarter_values() -> None:
    rows = []
    for hour in range(24):
        for minute in (0, 15, 30, 45):
            rows.append(
                f"<tr><td>24.08.2026 {hour:02d}:{minute:02d}</td><td>0,080</td></tr>"
            )
    readings = api.LinzNetzClient._parse_readings("<table>" + "".join(rows) + "</table>")
    assert len(readings) == 96
    assert {item.start_local.date() for item in readings} == {date(2026, 8, 24)}


def test_returned_day_fail_closed_is_unchanged() -> None:
    wrong = api.QuarterReading(datetime(2026, 8, 24, 0, 0), 0.1)
    with pytest.raises(api.LinzNetzParseError, match="requested=2026-08-21"):
        fix.BrowserContractLinzNetzClient._validate_requested_day(
            date(2026, 8, 21), [wrong]
        )


def test_pagination_payload_is_unchanged() -> None:
    pagination = api.PaginationInfo("myForm1:consumptionsTable", 24, 96)
    payload = fix.BrowserContractLinzNetzClient._build_pagination_payload(
        {"myForm1": "myForm1", "jakarta.faces.partial.ajax": "true"},
        pagination,
        24,
        "jakarta.faces.ViewState",
        "state-2",
    )
    assert payload["jakarta.faces.source"] == "myForm1:consumptionsTable"
    assert payload["jakarta.faces.partial.execute"] == "myForm1:consumptionsTable"
    assert payload["jakarta.faces.partial.render"] == "myForm1:consumptionsTable"
