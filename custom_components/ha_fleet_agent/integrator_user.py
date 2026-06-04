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
        """Legt den Wartungs-User an — oder uebernimmt einen bereits vorhandenen.

        Findet sich im `homeassistant`-Auth-Provider schon ein Eintrag fuer
        INTEGRATOR_USERNAME (etwa nach einem Plugin-Reinstall mit neuer entry_id
        oder einem verlorenen lokalen Store, der HA-User selbst aber noch da),
        wuerde ``add_auth`` mit ``InvalidUsername: username_already_exists``
        abbrechen — genau der Fehler nach dem HACS-Update. Wir erkennen den Fall
        vorab und rotieren dann nur das Passwort, statt einen zweiten User
        anzulegen. Sonst haeuften sich bei jedem Reload Karteileichen und der
        Tunnel bekaeme nie wieder gueltige Credentials.
        """
        provider = self._hass.auth.get_auth_provider(_HA_AUTH_PROVIDER_TYPE, None)
        if provider is None:
            raise RuntimeError(
                "Kein 'homeassistant'-Auth-Provider verfuegbar — Wartungs-User "
                "kann ohne Username/Passwort-Anmeldung nicht genutzt werden."
            )

        password = secrets.token_urlsafe(24)

        if self._auth_entry_exists(provider):
            user = await self._reuse_integrator_login(provider, password)
        else:
            user = await self._create_integrator_login(provider, password)

        self._credentials = IntegratorCredentials(
            user_id=user.id,
            username=INTEGRATOR_USERNAME,
            password=password,
        )
        await self._store.async_save({"user_id": user.id, "password": password})

    def _auth_entry_exists(self, provider: Any) -> bool:
        """True, wenn der `homeassistant`-Provider den Integrator-Username kennt.

        Vergleicht normalisiert (HA speichert Usernamen ge-casefold-et), damit
        der Check genau das trifft, woran ``add_auth`` scheitern wuerde.
        """
        normalize = provider.data.normalize_username
        target = normalize(INTEGRATOR_USERNAME)
        return any(
            normalize(entry["username"]) == target for entry in provider.data.users
        )

    async def _create_integrator_login(self, provider: Any, password: str) -> Any:
        """Erstinstallation: User + Auth-Eintrag komplett neu anlegen."""
        user = await self._hass.auth.async_create_user(
            name=INTEGRATOR_USER_NAME,
            group_ids=["system-admin"],
            local_only=False,
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
        _LOGGER.info(
            "Wartungs-User '%s' angelegt (user_id=%s)", INTEGRATOR_USERNAME, user.id
        )
        return user

    async def _reuse_integrator_login(self, provider: Any, password: str) -> Any:
        """Vorhandenen Integrator-Login uebernehmen: Passwort rotieren.

        Das alte Passwort ist ohne den lokalen Store nicht mehr bekannt, also
        setzt ``change_password`` ein neues. Existiert noch der verknuepfte
        HA-User, wird er wiederverwendet; ist der Auth-Eintrag verwaist (kein
        User mehr verknuepft), legen wir den User nach und binden ihn an die
        vorhandenen Credentials.
        """
        await self._hass.async_add_executor_job(
            provider.data.change_password, INTEGRATOR_USERNAME, password
        )
        await provider.data.async_save()

        credentials = await provider.async_get_or_create_credentials(
            {"username": INTEGRATOR_USERNAME}
        )
        user = await self._hass.auth.async_get_user_by_credentials(credentials)
        if user is not None:
            _LOGGER.info(
                "Bestehenden Wartungs-User '%s' uebernommen, Passwort rotiert "
                "(user_id=%s)",
                INTEGRATOR_USERNAME,
                user.id,
            )
            return user

        user = await self._hass.auth.async_create_user(
            name=INTEGRATOR_USER_NAME,
            group_ids=["system-admin"],
            local_only=False,
        )
        await self._hass.auth.async_link_user(user, credentials)
        _LOGGER.info(
            "Verwaisten Integrator-Auth-Eintrag an neuen User '%s' gebunden "
            "(user_id=%s)",
            INTEGRATOR_USERNAME,
            user.id,
        )
        return user

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
