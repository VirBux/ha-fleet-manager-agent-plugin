"""Repair-Flow für eingehende Verbindungsanfragen (REQUIREMENTS §4.2).

Endkunde sieht im HA-Dashboard einen Reparatur-Eintrag mit "Beheben"-Button.
Der Flow zeigt Betreff/Grund/Dauer und bietet zwei Aktionen:
  - Annehmen  → optional Dauer anpassen, dann confirm_request(accepted=True)
  - Ablehnen  → direkt confirm_request(accepted=False)

Der Issue wird vom RemoteAccessManager beim Eintreffen einer Anfrage
erstellt (`_on_connection_request`) und nach Bestätigung/Ablehnung
gelöscht (`_dismiss_notification`).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import DATA_REMOTE_ACCESS, DOMAIN, MAX_SESSION_HOURS
from .remote_access import RemoteAccessManager


class ConnectionRequestRepairFlow(RepairsFlow):
    """Wizard, mit dem der Endkunde eine Verbindungsanfrage annimmt/ablehnt."""

    def __init__(
        self,
        manager: RemoteAccessManager,
        request_id: str,
        subject: str,
        reason: str,
        requested_hours: int,
    ) -> None:
        self._manager = manager
        self._request_id = request_id
        self._subject = subject or "-"
        self._reason = reason or "-"
        self._requested_hours = max(1, min(int(requested_hours or MAX_SESSION_HOURS), MAX_SESSION_HOURS))

    @property
    def _placeholders(self) -> dict[str, str]:
        return {
            "subject": self._subject,
            "reason": self._reason,
            "requested_hours": str(self._requested_hours),
        }

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Einstieg: Menu mit Annehmen / Ablehnen."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["accept", "reject"],
            description_placeholders=self._placeholders,
        )

    async def async_step_accept(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Dauer bestätigen / anpassen → confirm_request(accepted=True)."""
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(
                        "duration_hours", default=self._requested_hours
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=MAX_SESSION_HOURS,
                            step=1,
                            unit_of_measurement="h",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            )
            return self.async_show_form(
                step_id="accept",
                data_schema=schema,
                description_placeholders=self._placeholders,
            )

        duration = int(user_input["duration_hours"])
        await self._manager.confirm_request(
            request_id=self._request_id,
            accepted=True,
            duration_hours=duration,
        )
        return self.async_create_entry(title="", data={})

    async def async_step_reject(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Direkt ablehnen — kein weiterer Schritt nötig."""
        await self._manager.confirm_request(
            request_id=self._request_id,
            accepted=False,
        )
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Wird von HA aufgerufen, wenn der Nutzer auf "Beheben" klickt."""
    payload = data or {}
    entry_id: str = payload.get("entry_id", "")
    request_id: str = payload.get("request_id", "")

    manager: RemoteAccessManager = hass.data[DOMAIN][entry_id][DATA_REMOTE_ACCESS]

    return ConnectionRequestRepairFlow(
        manager=manager,
        request_id=request_id,
        subject=payload.get("subject", ""),
        reason=payload.get("reason", ""),
        requested_hours=payload.get("duration_hours", MAX_SESSION_HOURS),
    )
