"""Verwaltung des dedizierten HA-Wartungs-Users `ha-fleet-integrator`.

Verantwortlichkeiten (REQUIREMENTS §4.4):

- Beim erstmaligen Setup einen Admin-User in Home Assistant anlegen und ein
  zufaelliges Passwort via `homeassistant`-Auth-Provider hinterlegen.
- Credentials verschluesselt nichts: HA verschluesselt das Passwort selbst im
  Auth-Storage. Wir persistieren das **Klartext-Passwort** lokal via
  ``homeassistant.helpers.storage.Store`` — denn beim Tunnel-Aufbau muessen
  wir es an den Integrator (ueber das Backend) durchreichen koennen. Der Store
  liegt im HA-Storage-Verzeichnis (`.storage/...`), Zugriff hat nur Root/HA.
- **Fail-Closed (#110):** Der User ruht standardmaessig **deaktiviert**
  (`is_active=False`) und wird nur fuer die Dauer einer aktiven Wartungs-Session
  scharf geschaltet — ``async_activate`` (Session-Start: Passwort rotieren +
  aktivieren) / ``async_deactivate`` (Session-Ende: Refresh-Tokens entfernen +
  deaktivieren). So ist der Admin-Account ausserhalb einer Freigabe wertlos,
  selbst wenn die HA-Instanz von aussen (Nabu Casa, Port-Forward, …) erreichbar
  ist. ``async_refresh_status`` bleibt als defensive Pruefung vor dem Tunnel.

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
        """Stellt sicher, dass der Wartungs-User existiert und **deaktiviert ruht**.

        Fail-Closed (REQUIREMENTS §4.4 / #110): Der User wird beim Setup immer auf
        ``is_active=False`` gesetzt. Eine Wartungs-Session schaltet ihn per
        ``async_activate`` gezielt scharf und ``async_deactivate`` am Ende wieder
        aus. So ist der Account ausserhalb einer aktiven Freigabe wertlos — auch
        wenn die HA-Instanz von aussen erreichbar ist. Da Sessions nur in-memory
        leben, ist beim Setup nie eine aktiv; "immer deaktivieren" ist korrekt und
        deckt zugleich Crash-Recovery und die Migration von Bestandsinstallationen
        (heute dauerhaft aktiv) ab.
        """
        data = await self._store.async_load()
        if isinstance(data, dict) and data.get("user_id") and data.get("password"):
            user = await self._hass.auth.async_get_user(data["user_id"])
            if user is not None:
                self._credentials = IntegratorCredentials(
                    user_id=user.id,
                    username=INTEGRATOR_USERNAME,
                    password=data["password"],
                )
                await self._ensure_deactivated(user)
                _LOGGER.debug(
                    "Wartungs-User aus Storage geladen, ruht deaktiviert: %s", user.id
                )
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
        # Fail-Closed (#110): der frisch angelegte User ruht deaktiviert, bis eine
        # Wartungs-Session ihn ueber async_activate scharf schaltet.
        await self._ensure_deactivated(user)

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
            # Fail-Closed (#110): ein beibehaltener User MUSS deaktiviert ruhen —
            # sonst bliebe nach dem Unload ein dauerhaft aktiver Admin-Account.
            await self._ensure_deactivated()
            _LOGGER.info(
                "Wartungs-User wird beibehalten und deaktiviert (entry_id=%s)",
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

    async def _ensure_deactivated(self, user: Any | None = None) -> None:
        """Setzt den Wartungs-User auf ``is_active=False`` (fail-closed, idempotent).

        ``user`` kann direkt uebergeben werden (spart einen Lookup); sonst wird er
        ueber die persistierte ``user_id`` aufgeloest.
        """
        if user is None:
            if self._credentials is None:
                return
            user = await self._hass.auth.async_get_user(self._credentials.user_id)
        if user is None:
            return
        if getattr(user, "is_active", False):
            await self._hass.auth.async_update_user(user, is_active=False)
            _LOGGER.debug("Wartungs-User deaktiviert (fail-closed)")

    async def async_activate(self) -> IntegratorCredentials | None:
        """Session-Start: Passwort rotieren (Phase 2), User aktivieren, Credentials liefern.

        Rueckgabe: die aktiven Credentials (mit frischem Passwort) oder — wenn der
        User nicht mehr existiert — ein Credentials-Objekt mit ``error`` gesetzt
        (der Aufrufer oeffnet dann keine Session).
        """
        if self._credentials is None:
            _LOGGER.error(
                "async_activate ohne Credentials — Wartungs-User nicht eingerichtet"
            )
            return None

        user = await self._hass.auth.async_get_user(self._credentials.user_id)
        if user is None:
            _LOGGER.warning(
                "Wartungs-User '%s' nicht mehr vorhanden — Aktivierung nicht moeglich",
                INTEGRATOR_USERNAME,
            )
            self._credentials = IntegratorCredentials(
                user_id=self._credentials.user_id,
                username=INTEGRATOR_USERNAME,
                password="",
                active=False,
                error="user_missing",
            )
            return self._credentials

        # Phase 2 — Passwort pro Session rotieren: ein evtl. frueher geleaktes
        # Passwort ist nach Session-Ende wertlos. Bei einem Provider-Problem
        # bleibt das bisherige Passwort gueltig (Verfuegbarkeit > Rotation).
        password = self._credentials.password
        provider = self._hass.auth.get_auth_provider(_HA_AUTH_PROVIDER_TYPE, None)
        if provider is not None:
            new_password = secrets.token_urlsafe(24)
            try:
                await self._hass.async_add_executor_job(
                    provider.data.change_password, INTEGRATOR_USERNAME, new_password
                )
                await provider.data.async_save()
                password = new_password
            except Exception:  # noqa: BLE001 — Rotation best-effort, Session laeuft weiter
                _LOGGER.warning(
                    "Passwort-Rotation fehlgeschlagen — nutze bisheriges Passwort",
                    exc_info=True,
                )

        await self._hass.auth.async_update_user(user, is_active=True)
        await self._store.async_save({"user_id": user.id, "password": password})
        self._credentials = IntegratorCredentials(
            user_id=user.id,
            username=INTEGRATOR_USERNAME,
            password=password,
            active=True,
        )
        _LOGGER.info("Wartungs-User aktiviert + Passwort rotiert (Session-Start)")
        return self._credentials

    async def async_deactivate(self) -> None:
        """Session-Ende: alle Refresh-Tokens des Users entfernen + ``is_active=False``.

        Das Entfernen der Refresh-Tokens beendet eine noch offene Integrator-
        Browser-Session **sofort** (statt erst nach Access-Token-Ablauf ~30 min).
        """
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
            return

        for token in list(getattr(user, "refresh_tokens", {}).values()):
            try:
                await self._hass.auth.async_remove_refresh_token(token)
            except Exception:  # noqa: BLE001 — Best-effort Token-Cleanup
                _LOGGER.debug(
                    "Refresh-Token konnte nicht entfernt werden", exc_info=True
                )

        if getattr(user, "is_active", False):
            await self._hass.auth.async_update_user(user, is_active=False)

        # Passwort im Store bleibt liegen (wird beim naechsten Aktivieren rotiert);
        # in den In-Memory-Credentials leeren wir es defensiv.
        self._credentials = IntegratorCredentials(
            user_id=user.id,
            username=INTEGRATOR_USERNAME,
            password="",
            active=False,
        )
        _LOGGER.info(
            "Wartungs-User deaktiviert + Refresh-Tokens entfernt (Session-Ende)"
        )
