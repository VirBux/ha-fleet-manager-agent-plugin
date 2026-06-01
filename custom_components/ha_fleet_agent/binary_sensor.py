"""Binary-Sensor-Plattform: Tunnel-Status.

Ein einziger Binary-Sensor „Tunnel aktiv" (Device-Class `connectivity`)
spiegelt den Zustand der WebSocket-Verbindung zum Connector. Reagiert
reaktiv auf SIGNAL_TUNNEL_STATE, das der TunnelForwarder beim Auf- und
Abbau feuert (REQUIREMENTS §4.4 — Endkunden-Abbruch).
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_CLIENT, DATA_DEVICE_INFO, DOMAIN, SIGNAL_TUNNEL_STATE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bucket = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [TunnelActiveBinarySensor(entry.entry_id, bucket[DATA_CLIENT], bucket[DATA_DEVICE_INFO])]
    )


class TunnelActiveBinarySensor(BinarySensorEntity):
    """Zeigt, ob aktuell ein WS-Tunnel zum Connector offen ist."""

    _attr_has_entity_name = True
    _attr_name = "Tunnel aktiv"
    _attr_icon = "mdi:tunnel"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry_id: str, ws_client, device_info) -> None:
        self._entry_id = entry_id
        self._ws_client = ws_client
        self._attr_unique_id = f"{entry_id}_tunnel_active"
        self._attr_device_info = device_info
        # Initialer Wert aus dem WS-Client — Signal pflegt es danach
        self._is_open: bool = bool(ws_client.is_connected)

    @property
    def is_on(self) -> bool:
        return self._is_open

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_TUNNEL_STATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self, entry_id: str, open_: bool) -> None:
        if entry_id != self._entry_id:
            return
        self._is_open = bool(open_)
        self.async_write_ha_state()
