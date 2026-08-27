"""Browser-contract aligned LINZ NETZ portal client.

This module keeps the existing parser/backfill/statistics behavior from api.py and
only overrides the date-selection and final display request construction according
to the browser contracts confirmed live in 0.1.14.
"""
from __future__ import annotations

from datetime import date
import logging
import re

from bs4 import BeautifulSoup, Tag

from .assign_to_date_detail_diagnostics import assign_to_date_detail_contract
from .api import (
    AjaxBehavior,
    ChoiceField,
    LinzNetzClient,
    LinzNetzParseError,
    _FROM_RE,
    _TO_RE,
    _VIEW_STATE_RE,
)
from .const import PORTAL_URL
from .inline_date_handler_diagnostics import inline_handler_contract

_LOGGER = logging.getLogger(__name__)
_TO_VIEW_STATE_RE = re.compile(r"(?:jakarta|javax)\.faces\.ViewState", re.IGNORECASE)


class BrowserContractLinzNetzClient(LinzNetzClient):
    """Apply the confirmed From-onchange and display browser contracts."""

    async def _async_select_day(
        self,
        *,
        behavior_soups: list[BeautifulSoup],
        form: Tag,
        form_id: str,
        date_from: Tag,
        date_to: Tag,
        quarter: ChoiceField,
        kwh: ChoiceField,
        requested_day: date,
        view_state_name: str,
        view_state_value: str,
    ) -> tuple[str, Tag, bool]:
        """Execute the unchanged From onchange, then the confirmed To AJAX."""
        current_form = BeautifulSoup(str(form), "html.parser").find("form")
        if current_form is None:
            raise LinzNetzParseError("Verbrauchsformular konnte nicht kopiert werden")

        current_from = self._find_named_control(current_form, _FROM_RE)
        current_to = self._find_named_control(current_form, _TO_RE)
        if current_from is None or current_to is None:
            raise LinzNetzParseError("Datumsfelder nach Viertelstunden-Auswahl nicht gefunden")
        if not current_from.get("name") or not current_to.get("name"):
            raise LinzNetzParseError("Wirksame Datumsfeldnamen nicht gefunden")

        day_text = requested_day.strftime("%d.%m.%Y")
        current_to["value"] = day_text
        current_from["value"] = day_text

        to_contract: dict[str, object] | None = None
        to_form: Tag | None = None
        for behavior_soup in behavior_soups:
            candidate = assign_to_date_detail_contract(str(behavior_soup))
            candidate_form_id = candidate.get("f")
            if not candidate.get("found") or not isinstance(candidate_form_id, str):
                continue
            candidate_form = behavior_soup.find("form", id=candidate_form_id)
            if candidate_form is None:
                candidate_form = behavior_soup.find(
                    "form", attrs={"name": candidate_form_id}
                )
            if isinstance(candidate_form, Tag):
                to_contract = candidate
                to_form = candidate_form
                break
        if to_contract is None or to_form is None:
            raise LinzNetzParseError(
                "assignToDate-PrimeFaces-Form-Contract nicht gefunden"
            )

        to_form_id = str(to_contract.get("f"))
        to_source = to_contract.get("source")
        attr_name = to_contract.get("render_attr_name")
        attr_operator = to_contract.get("render_attr_operator")
        attr_value = to_contract.get("render_attr_value")
        to_render = f"@([{attr_name}{attr_operator}{attr_value}])"
        if not (
            to_contract.get("ajax_type") == "PrimeFaces.ab"
            and to_contract.get("ajax_direct") is True
            and isinstance(to_source, str)
            and to_source.startswith(f"{to_form_id}:")
            and to_contract.get("f_role") == "form"
            and to_contract.get("render_selector_kind") == "primefaces_attribute_search"
            and attr_name == "id"
            and attr_operator == "$="
            and attr_value == "panel_calendarToRegion"
            and len(to_render) == to_contract.get("render_length")
            and to_contract.get("params_arguments_mode") == "indexed_arguments"
            and to_contract.get("params_argument_indexes") == [0]
            and to_contract.get("assign_call_arities") == [1]
            and to_contract.get("assign_call_arg_kinds") == ["array"]
            and to_contract.get("assign_call_param_names") == ["assignToDate"]
            and to_contract.get("assign_call_param_value_roles")
            == ["assignToDate:this_value"]
            and to_contract.get("execute_present") is False
            and to_contract.get("event_present") is False
        ):
            raise LinzNetzParseError(
                "assignToDate entspricht nicht dem bestätigten PrimeFaces-Contract"
            )

        to_view_state = to_form.find("input", attrs={"name": _VIEW_STATE_RE})
        if to_view_state is None or not to_view_state.get("name"):
            raise LinzNetzParseError(
                "JSF ViewState im assignToDate-Formular nicht gefunden"
            )

        contract = inline_handler_contract(current_from, "onchange")
        if not contract["primefaces_ajax"]:
            raise LinzNetzParseError("PrimeFaces-AJAX für From-Datumsfeld nicht gefunden")

        execute = str(contract.get("execute") or "")
        render = str(contract.get("render") or "")
        event = str(contract.get("event") or "")
        source = contract.get("source")

        from_name = str(current_from.get("name"))
        if execute != from_name or render != form_id or event.lower() != "change":
            raise LinzNetzParseError(
                "From-onchange entspricht nicht dem bestätigten Browser-Contract"
            )

        payload = self._collect_form_payload(current_form)
        payload.update(
            {
                "jakarta.faces.partial.ajax": "true",
                "jakarta.faces.partial.execute": execute,
                "jakarta.faces.partial.render": render,
                "jakarta.faces.behavior.event": event,
                form_id: form_id,
                quarter.name: quarter.value,
                kwh.name: kwh.value,
                view_state_name: view_state_value,
            }
        )
        # 0.1.14 live markup did not explicitly render a PrimeFaces source for the
        # From onchange. Do not invent one. If a future portal version renders an
        # explicit source, carry that exact value through.
        if isinstance(source, str) and source:
            payload["jakarta.faces.source"] = source

        _LOGGER.debug(
            "LINZ NETZ From-onchange: requested=%s source=%s execute=%s render=%s event=%s",
            requested_day.isoformat(),
            source or "none",
            execute,
            render,
            event,
        )

        result = await self._session.post(
            PORTAL_URL,
            data=payload,
            headers={
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=True,
        )
        result.raise_for_status()
        body = await result.text()
        updates = self._parse_partial_updates(body)
        _LOGGER.debug(
            "LINZ NETZ From-onchange Partial Response: requested=%s updates=%s ids=%s",
            requested_day.isoformat(),
            len(updates),
            self._safe_partial_update_ids(updates),
        )

        current_view_state = self._extract_partial_view_state(body) or view_state_value
        current_form = self._merge_partial_response_form(current_form, form_id, body)

        # The browser edits From first. Its AJAX re-renders myForm1, but the portal
        # can legitimately leave To at its previous value until assignToDate runs.
        self._verify_rendered_day_if_present(current_form, _FROM_RE, requested_day)

        # JSF's browser client propagates every partial ViewState update to all
        # forms on the page. Mirror that behavior before submitting the separate
        # assignToDate form; its original hidden value is stale after From AJAX.
        to_view_state["value"] = current_view_state

        to_payload = self._collect_form_payload(to_form)
        to_payload.update(
            {
                "jakarta.faces.partial.ajax": "true",
                "jakarta.faces.source": to_source,
                to_source: to_source,
                "jakarta.faces.partial.render": to_render,
                "assignToDate": day_text,
                to_form_id: to_form_id,
            }
        )
        to_result = await self._session.post(
            PORTAL_URL,
            data=to_payload,
            headers={
                "Faces-Request": "partial/ajax",
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=True,
        )
        to_result.raise_for_status()
        to_body = await to_result.text()
        to_updates = self._parse_partial_updates(to_body)
        to_view_state_value: str | None = None
        for update_id, update_value in to_updates.items():
            if _TO_VIEW_STATE_RE.search(update_id):
                to_view_state_value = update_value
                break
        if to_view_state_value is None:
            raise LinzNetzParseError(
                "assignToDate-AJAX lieferte keinen JSF-ViewState-Update-Knoten"
            )
        current_view_state = to_view_state_value or current_view_state
        current_form = self._merge_partial_response_form(
            current_form, form_id, to_body
        )

        # Only after both browser events may the range be considered selected.
        self._verify_rendered_day_if_present(current_form, _FROM_RE, requested_day)
        self._verify_rendered_day_if_present(current_form, _TO_RE, requested_day)

        return current_view_state, current_form, True

    @classmethod
    def _build_display_payload(
        cls,
        form: Tag,
        form_id: str,
        button_id: str,
        date_from: Tag,
        date_to: Tag,
        quarter: ChoiceField,
        kwh: ChoiceField,
        day_text: str,
        view_state_name: str,
        view_state_value: str,
    ) -> dict[str, str]:
        """Build the confirmed button-only PrimeFaces display request."""
        del date_from, date_to, day_text

        payload = cls._collect_form_payload(form)
        payload.update(
            {
                "jakarta.faces.partial.ajax": "true",
                "jakarta.faces.source": button_id,
                "jakarta.faces.partial.execute": button_id,
                "jakarta.faces.partial.render": cls._find_render_target(form, form_id),
                "jakarta.faces.behavior.event": "action",
                form_id: form_id,
                quarter.name: quarter.value,
                kwh.name: kwh.value,
                view_state_name: view_state_value,
            }
        )
        return payload
