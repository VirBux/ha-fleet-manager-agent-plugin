"""Sensor-Plattform: Verbindungs- und Fernzugriffs-Status.

Vier Read-only-Sensoren:

- Backend-Verbindungsstatus  (Diagnose-Sektion)
  Zeigt "connected" wenn der letzte State-POST ans Backend erfolgreich war,
  sonst "disconnected". Grundlage: SIGNAL_CONNECTION_STATE vom StateReporter.
  Anmerkung (Phase 4): Früher spiegelte dieser Sensor den WS-Connection-State
  wider. Jetzt zeigt er den letzten REST-Push-Erfolg — semantisch äquivalent
  aus Sicht des Endkunden ("ist das Plugin mit dem Backend verbunden?").

- Fernzugriffs-Status                 (Hauptanzeige)
- Ablaufzeit der Vorab-Freigabe       (Hauptanzeige, Timestamp)
- Endzeit der laufenden Session       (Hauptanzeige, Timestamp)
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_DEVICE_INFO,
    DATA_REMOTE_ACCESS,
    DOMAIN,
    SIGNAL_CONNECTION_STATE,
    SIGNAL_REMOTE_ACCESS_STATE,
    STATUS_IDLE,
)


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
            FleetConnectionSensor(entry.entry_id, device_info),
            RemoteAccessStatusSensor(entry.entry_id, remote_access, device_info),
            PreAuthExpiresSensor(entry.entry_id, remote_access, device_info),
            SessionEndsSensor(entry.entry_id, remote_access, device_info),
        ]
    )


# ----------------------------------------------------------- Connection

class FleetConnectionSensor(SensorEntity):
    """Spiegelt den Backend-Verbindungsstatus (letzter State-Push erfolgreich).

    Das Signal SIGNAL_CONNECTION_STATE wird vom StateReporter nach jedem
    POST-Versuch gesendet: True = 2xx, False = Fehler/Timeout.
    """

    _attr_has_entity_name = True
    _attr_name = "Verbindungsstatus"
    _attr_icon = "mdi:cloud-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry_id: str, device_info) -> None:
        self._entry_id = entry_id
        self._attr_unique_id = f"{entry_id}_connection_state"
        self._attr_device_info = device_info
        # Initial: unbekannt (noch kein Push versucht)
        self._connected = False

    @property
    def native_value(self) -> str:
        return "connected" if self._connected else "disconnected"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CONNECTION_STATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self, entry_id: str, connected: bool) -> None:
        if entry_id != self._entry_id:
            return
        self._connected = connected
        self.async_write_ha_state()


# ----------------------------------------------------------- Remote access

class _RemoteAccessSensorBase(SensorEntity):
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


class RemoteAccessStatusSensor(_RemoteAccessSensorBase):
    """Aggregierter Status: idle | pre_authorized | session_active."""

    _attr_name = "Fernzugriffs-Status"
    _attr_icon = "mdi:access-point-network"
    _attr_options = ["idle", "pre_authorized", "session_active"]
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_remote_access_status"

    @property
    def native_value(self) -> str:
        return self._remote_access.status or STATUS_IDLE


class PreAuthExpiresSensor(_RemoteAccessSensorBase):
    """Ablaufzeitpunkt der aktiven Vorab-Freigabe."""

    _attr_name = "Vorab-Freigabe läuft ab"
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_preauth_expires_at"

    @property
    def native_value(self) -> datetime | None:
        pre_auth = self._remote_access.pre_authorization
        return pre_auth.expires_at if pre_auth else None


class SessionEndsSensor(_RemoteAccessSensorBase):
    """Endzeitpunkt der laufenden Fernzugriffs-Session."""

    _attr_name = "Aktive Sitzung endet"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_session_ends_at"

    @property
    def native_value(self) -> datetime | None:
        session = self._remote_access.session
        return session.ends_at() if session else None
