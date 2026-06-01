"""Number-Plattform: Konfiguration der Vorab-Freigabe.

Beide Werte tragen `EntityCategory.CONFIG`, sodass HA sie auf der Geräteseite
in der Sektion „Konfiguration" gruppiert.

- Gültigkeitsdauer der Vorab-Freigabe (1–168 h)
- Maximale Sitzungsdauer einer einzelnen Verbindung (1–12 h)
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DATA_DEVICE_INFO,
    DATA_REMOTE_ACCESS,
    DOMAIN,
    MAX_PREAUTH_VALIDITY_HOURS,
    MAX_SESSION_HOURS,
    SIGNAL_REMOTE_ACCESS_STATE,
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
            PreAuthValidityNumber(entry.entry_id, remote_access, device_info),
            PreAuthMaxDurationNumber(entry.entry_id, remote_access, device_info),
        ]
    )


class _BaseNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = "h"
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

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


class PreAuthValidityNumber(_BaseNumber):
    """Wie lange die Vorab-Freigabe gilt (Default 8 h, max. 168 h)."""

    _attr_translation_key = "preauth_validity"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_PREAUTH_VALIDITY_HOURS

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_preauth_validity"

    @property
    def native_value(self) -> float:
        return self._remote_access.validity_hours

    async def async_set_native_value(self, value: float) -> None:
        await self._remote_access.set_validity_hours(value)


class PreAuthMaxDurationNumber(_BaseNumber):
    """Maximale Sitzungsdauer einer einzelnen Verbindung (1–12 h)."""

    _attr_translation_key = "preauth_max_duration"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_SESSION_HOURS

    def __init__(self, entry_id: str, remote_access, device_info) -> None:
        super().__init__(entry_id, remote_access, device_info)
        self._attr_unique_id = f"{entry_id}_preauth_max_duration"

    @property
    def native_value(self) -> float:
        return float(self._remote_access.max_duration_hours)

    async def async_set_native_value(self, value: float) -> None:
        await self._remote_access.set_max_duration_hours(int(value))
