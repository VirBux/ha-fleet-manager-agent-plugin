"""Verwaltung des dedizierten HA-Wartungs-Users `ha-fleet-integrator`.

Verantwortlichkeiten (REQUIREMENTS §4.4):

- Beim erstmaligen Setup einen Admin-User in Home Assistant anlegen und ein
  zufaelliges Passwort via `homeassistant`-Auth-Provider hinterlegen.
- Credentials verschluesselt nichts: HA verschluesselt das Passwort selbst im
  Auth-Storage. Wir persistieren das **Klartext-Passwort** lokal via
  ``homeassistant.helpers.storage.Store`` — denn beim Tunnel-Aufbau muessen
  wir es an den Integrator (ueber das Backend) durchreichen koennen. Der Store
  liegt im HA-Storage-Verzeichnis (`.storage/...`), Zugriff hat nur Root/HA.
- Auf User-Deaktivierung durch den Endkunden reagieren: vor jedem Tunnel-Aufbau
  pruefen ob der User noch aktiv ist; sonst ein Fehler-Flag im Frame setzen.

Die exakte HA-Auth-API hat in den letzten HA-Versionen mehrfach gewechselt;
wir halten uns hier an die fuer 2024+/2026 dokumentierten Methoden:
``hass.auth.async_create_user`` (async), ``hass.auth.get_auth_provider``
(synchron, @callback — *nicht* ``async_get_provider``, die gibt es nicht!),
``provider.data.add_auth``, ``provider.data.async_save``.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    INTEGRATOR_USER_NAME,
    INTEGRATOR_USER_STORAGE_KEY,
    INTEGRATOR_USERNAME,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Home-Assistant-Auth-Provider-Typ fuer Username/Passwort-Logins.
_HA_AUTH_PROVIDER_TYPE = "homeassistant"


@dataclass
class IntegratorCredentials:
    """Wartungs-User Zugangsdaten + Lebenszyklus-Info.

    Das Klartext-Passwort wird per ``field(repr=False)`` aus der automatischen
    ``__repr__`` ausgeklammert — Defense-in-Depth gegen versehentliches Loggen
    des ganzen Objekts (z.B. via Exception-Trace oder ``_LOGGER.x("%s", creds)``).
    Funktional unveraendert: ``credentials.password`` liefert weiterhin den Wert.
    """

    user_id: str
    username: str
    password: str = field(repr=False)
    active: bool = True
    error: str | None = None

    def to_frame_dict(self) -> dict[str, Any]:
        """Konvertiert in das `tunnel_credentials`-Frame-Format."""
        payload: dict[str, Any] = {"username": self.username}
        if self.error is not None:
            payload["error"] = self.error
        else:
            payload["password"] = self.password
        return payload


class IntegratorUserManager:
    """Legt den `ha-fleet-integrator`-User an und verwaltet seinen Lebenszyklus."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{INTEGRATOR_USER_STORAGE_KEY}.{entry_id}"
        )
        self._credentials: IntegratorCredentials | None = None

    @property
    def credentials(self) -> IntegratorCredentials | None:
        return self._credentials

    async def async_setup(self) -> None:
        """Stellt sicher, dass der Wartungs-User existiert und Credentials persistiert sind."""
        data = await self._store.async_load()
        if isinstance(data, dict) and data.get("user_id") and data.get("password"):
            user = await self._hass.auth.async_get_user(data["user_id"])
            if user is not None and not user.is_active:
                _LOGGER.warning(
                    "Wartungs-User '%s' ist deaktiviert — Tunnel-Sessions sind eingeschraenkt",
                    INTEGRATOR_USERNAME,
                )
                self._credentials = IntegratorCredentials(
                    user_id=data["user_id"],
                    username=INTEGRATOR_USERNAME,
                    password="",
                    active=False,
                    error="user_disabled",
                )
                return
            if user is not None:
                self._credentials = IntegratorCredentials(
                    user_id=user.id,
                    username=INTEGRATOR_USERNAME,
                    password=data["password"],
                )
                _LOGGER.debug("Wartungs-User aus Storage geladen: %s", user.id)
                return
            _LOGGER.info(
                "Persistierter Wartungs-User nicht mehr vorhanden — wird neu angelegt"
            )

        await self._create_user()

    async def _create_user(self) -> None:
        password = secrets.token_urlsafe(24)
        user = await self._hass.auth.async_create_user(
            name=INTEGRATOR_USER_NAME,
            group_ids=["system-admin"],
            local_only=False,
        )

        provider = self._hass.auth.get_auth_provider(_HA_AUTH_PROVIDER_TYPE, None)
        if provider is None:
            raise RuntimeError(
                "Kein 'homeassistant'-Auth-Provider verfuegbar — Wartungs-User "
                "kann ohne Username/Passwort-Anmeldung nicht genutzt werden."
            )
        # `add_auth` ist synchron, `async_save` persistiert in `.storage/auth_provider…`.
        await self._hass.async_add_executor_job(
            provider.data.add_auth, INTEGRATOR_USERNAME, password
        )
        await provider.data.async_save()

        # Credentials des Auth-Providers mit dem User verknuepfen, damit der Login funktioniert.
        credentials = await provider.async_get_or_create_credentials(
            {"username": INTEGRATOR_USERNAME}
        )
        await self._hass.auth.async_link_user(user, credentials)

        self._credentials = IntegratorCredentials(
            user_id=user.id,
            username=INTEGRATOR_USERNAME,
            password=password,
        )
        await self._store.async_save(
            {"user_id": user.id, "password": password}
        )
        _LOGGER.info(
            "Wartungs-User '%s' angelegt (user_id=%s)", INTEGRATOR_USERNAME, user.id
        )

    async def async_remove(self, keep_user: bool) -> None:
        """Entfernt den Wartungs-User (sofern `keep_user` False)."""
        if self._credentials is None:
            return
        if keep_user:
            _LOGGER.info(
                "Wartungs-User wird auf Wunsch des Endkunden beibehalten "
                "(entry_id=%s)",
                self._entry_id,
            )
            return

        user = await self._hass.auth.async_get_user(self._credentials.user_id)
        if user is not None:
            try:
                await self._hass.auth.async_remove_user(user)
            except Exception:  # noqa: BLE001 — Best-effort Cleanup
                _LOGGER.warning(
                    "Konnte Wartungs-User nicht entfernen", exc_info=True
                )

        provider = self._hass.auth.get_auth_provider(_HA_AUTH_PROVIDER_TYPE, None)
        if provider is not None:
            try:
                await self._hass.async_add_executor_job(
                    provider.data.async_remove_auth, INTEGRATOR_USERNAME
                )
                await provider.data.async_save()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Auth-Eintrag bereits entfernt", exc_info=True)

        await self._store.async_remove()
        self._credentials = None

    async def async_refresh_status(self) -> None:
        """Prueft vor jedem Tunnel-Aufbau, ob der User noch aktiv ist."""
        if self._credentials is None:
            return
        user = await self._hass.auth.async_get_user(self._credentials.user_id)
        if user is None:
            self._credentials = IntegratorCredentials(
                user_id=self._credentials.user_id,
                username=INTEGRATOR_USERNAME,
                password="",
                active=False,
                error="user_missing",
            )
        elif not user.is_active:
            self._credentials = IntegratorCredentials(
                user_id=self._credentials.user_id,
                username=INTEGRATOR_USERNAME,
                password="",
                active=False,
                error="user_disabled",
            )
