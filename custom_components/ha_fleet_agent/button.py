"""Button-Plattform: Aktiven Tunnel manuell trennen.

Erlaubt dem Endkunden, einen offenen Tunnel jederzeit zu kappen
(REQUIREMENTS §4.4 — Endkunden-Abbruch). Schließt zusätzlich
die laufende Wartungs-Session über den im TunnelForwarder
hinterlegten on_close-Callback.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_CLIENT,
    DATA_DEVICE_INFO,
    DOMAIN,
    SIGNAL_TUNNEL_STATE,
)

DATA_TUNNEL_FORWARDER = "tunnel_forwarder"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    bucket = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            CloseTunnelButton(
                entry.entry_id,
                bucket[DATA_CLIENT],
                bucket[DATA_TUNNEL_FORWARDER],
                bucket[DATA_DEVICE_INFO],
            )
        ]
    )


class CloseTunnelButton(ButtonEntity):
    """Trennt den aktiven Tunnel und beendet die Wartungs-Session."""

    _attr_has_entity_name = True
    _attr_translation_key = "close_tunnel"
    _attr_icon = "mdi:lan-disconnect"

    def __init__(self, entry_id: str, ws_client, tunnel_forwarder, device_info) -> None:
        self._entry_id = entry_id
        self._ws_client = ws_client
        self._tunnel_forwarder = tunnel_forwarder
        self._attr_unique_id = f"{entry_id}_close_tunnel"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        """Button ist nur drückbar, wenn auch wirklich ein Tunnel offen ist."""
        return bool(self._ws_client.is_connected)

    async def async_press(self) -> None:
        await self._tunnel_forwarder.async_close_tunnel()

    async def async_added_to_hass(self) -> None:
        # Verfügbarkeit reagiert auf SIGNAL_TUNNEL_STATE
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_TUNNEL_STATE, self._handle_update
            )
        )

    @callback
    def _handle_update(self, entry_id: str, _open: bool) -> None:
        if entry_id != self._entry_id:
            return
        self.async_write_ha_state()
