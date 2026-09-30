"""Switch-Plattform: Vorab-Freigabe (§4.3 REQUIREMENTS).

Schaltet die Vorab-Freigabe ein/aus. Die konkreten Werte (Gültigkeitsdauer und
maximale Sitzungsdauer) liegen in zwei zugehörigen `number`-Entities und werden
beim Einschalten aus dem `RemoteAccessManager` gezogen.

Seit #167 zusätzlich der Konfig-Switch „Gültigkeit ohne Ablaufdatum": Ist er an, gilt die
nächste Vorab-Freigabe bis zum Widerruf statt für die Gültigkeitsdauer. Wie die
Numbers ändert er eine bereits laufende Freigabe nicht.

Bewusst **kein** Switch "Fernzugriff manuell an/aus": ein dauerhafter manueller
Toggle widerspricht §4.2/§4.3 (Zugriff entsteht nur durch eine Vorab-Freigabe
oder durch Bestätigung einer konkreten Anfrage).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_DEVICE_INFO, DATA_REMOTE_ACCESS, DOMAIN, SIGNAL_REMOTE_ACCESS_STATE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bucket = hass.data[DOMAIN][entry.entry_id]
    remote_access = bucket[DATA_REMOTE_ACCESS]
    device_info = bucket[DATA_DEVICE_INFO]
    async_add_entities(
        [
            PreAuthorizationSwitch(entry.entry_id, remote_access, device_info),
            PreAuthUnlimitedSwitch(entry.entry_id, remote_access, device_info),
        ]
    )


class _RemoteAccessSwitchBase(SwitchEntity):
    _attr_has_entity_name = True

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        self._entry_id = entry_id
        self._remote_access = remote_access
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_REMOTE_ACCESS_STATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self, entry_id: str, _payload: dict) -> None:
        if entry_id != self._entry_id:
            return
        self.async_write_ha_state()


class PreAuthorizationSwitch(_RemoteAccessSwitchBase):
    """Hauptschalter für die Vorab-Freigabe an den Integrator."""

    _attr_translation_key = "pre_authorization"
    _attr_icon = "mdi:shield-key-outline"

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_pre_authorization"

    @property
    def is_on(self) -> bool:
        return self._remote_access.is_pre_authorized

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "validity_hours": self._remote_access.validity_hours,
            "max_duration_hours": self._remote_access.max_duration_hours,
        }
        pre_auth = self._remote_access.pre_authorization
        if pre_auth is not None:
            # Die laufende Freigabe — unabhängig vom Konfig-Switch „Gültigkeit ohne Ablaufdatum",
            # der erst beim nächsten Erteilen greift (#167).
            attrs["unlimited"] = pre_auth.unlimited
            if pre_auth.expires_at is not None:
                attrs["expires_at"] = pre_auth.expires_at.isoformat()
        session = self._remote_access.session
        if session is not None:
            attrs["session_request_id"] = session.request_id
            attrs["session_ends_at"] = session.ends_at().isoformat()
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Erteilt die Vorab-Freigabe mit den aktuell konfigurierten Werten."""
        await self._remote_access.grant_pre_authorization(
            expires_in_hours=self._remote_access.validity_hours,
            max_duration_hours=self._remote_access.max_duration_hours,
            unlimited=self._remote_access.unlimited,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._remote_access.revoke_pre_authorization()


class PreAuthUnlimitedSwitch(_RemoteAccessSwitchBase):
    """„Gültigkeit ohne Ablaufdatum" (#167): Die nächste Vorab-Freigabe gilt bis zum Widerruf.

    Konfigurationswert wie Gültigkeitsdauer und maximale Sitzungsdauer — eine
    laufende Freigabe bleibt, wie sie ist. Jede einzelne Sitzung bleibt befristet.
    """

    _attr_translation_key = "preauth_unlimited"
    _attr_icon = "mdi:infinity"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_preauth_unlimited"

    @property
    def is_on(self) -> bool:
        return self._remote_access.unlimited

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._remote_access.set_unlimited(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._remote_access.set_unlimited(False)
