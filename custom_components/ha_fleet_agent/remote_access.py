"""Fernzugriffs-Logik: Vorab-Freigabe + Verbindungsanfrage-Bestätigung.

Modellierung gemäss REQUIREMENTS §4:

- §4.2 — Standardablauf: Integrator stellt Anfrage (Betreff, Dauer, Grund) →
  Endkunde sieht alles in einer Persistent Notification und bestätigt über
  den Service `ha_fleet_agent.confirm_request`.
- §4.3 — Vorab-Freigabe: Endkunde legt im Voraus ein Zeitfenster (Gültigkeit)
  und eine maximale Sessiondauer fest. Innerhalb des Fensters genehmigt der
  Agent eingehende Anfragen automatisch und kappt deren Dauer auf das Maximum.

Phase 4 (TODO #50.23): Connection-Response + Preauth-Announce per REST statt
WebSocket-Frame. Alle Backend-Calls nutzen die übergebene aiohttp.ClientSession.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_PREAUTH_MAX_HOURS,
    DEFAULT_PREAUTH_VALIDITY_HOURS,
    DOMAIN,
    ISSUE_ID_PREFIX,
    MAX_PREAUTH_VALIDITY_HOURS,
    MAX_SESSION_HOURS,
    SIGNAL_REMOTE_ACCESS_STATE,
    STATUS_IDLE,
    STATUS_PRE_AUTHORIZED,
    STATUS_SESSION_ACTIVE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .integrator_user import IntegratorUserManager


@dataclass
class PreAuthorization:
    """Vom Endkunden vorab erteilte Freigabe (§4.3)."""

    expires_at: datetime
    max_duration_hours: int

    def is_active(self, now: datetime | None = None) -> bool:
        return (now or dt_util.utcnow()) < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "max_duration_hours": self.max_duration_hours,
        }


@dataclass
class ActiveSession:
    """Eine aktive Fernzugriffs-Session."""

    request_id: str
    subject: str
    reason: str
    duration_hours: int
    started_at: datetime = field(default_factory=dt_util.utcnow)

    def ends_at(self) -> datetime:
        return self.started_at + timedelta(hours=self.duration_hours)


class RemoteAccessManager:
    """Verwaltet Vorab-Freigaben, Konfigurationswerte und Session-Lifecycle.

    Alle Backend-Kommunikation erfolgt per REST (kein WebSocket mehr).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        session: aiohttp.ClientSession,
        backend_url: str,
        api_key: str,
        integrator_user: IntegratorUserManager | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._session = session
        self._backend_url = backend_url.rstrip("/")
        self._api_key = api_key
        # Wartungs-User-Manager (#110): Session-Start aktiviert ihn (+ Passwort-
        # Rotation), Session-Ende deaktiviert ihn (+ Refresh-Token-Kill). Optional,
        # damit Unit-Tests den Manager ohne HA-Auth-Stack instanziieren koennen.
        self._integrator_user = integrator_user

        self._pre_auth: PreAuthorization | None = None
        self._session_obj: ActiveSession | None = None

        # request_id des aktuell offenen Repair-Issues (#90). Grundlage fuer das
        # Self-Healing: verwaiste Issues (Integrator-Abbruch/Ablauf) werden beim
        # naechsten Poll entfernt, sobald das Backend "nichts offen" meldet.
        self._open_request_id: str | None = None

        # Vom Endkunden konfigurierte Defaults — werden persistiert
        self._validity_hours: float = DEFAULT_PREAUTH_VALIDITY_HOURS
        self._max_duration_hours: int = DEFAULT_PREAUTH_MAX_HOURS
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}"
        )

        # Cancel-Handles für ausstehende Auto-Timer
        self._session_expire_cancel = None
        self._preauth_expire_cancel = None

    # --------------------------------------------------------- Setup

    async def async_load(self) -> None:
        """Lädt die persistierten Konfigurationswerte."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        try:
            self._validity_hours = float(
                data.get("validity_hours", DEFAULT_PREAUTH_VALIDITY_HOURS)
            )
            self._max_duration_hours = int(
                data.get("max_duration_hours", DEFAULT_PREAUTH_MAX_HOURS)
            )
        except (TypeError, ValueError):
            _LOGGER.warning("Persistierte Pre-Auth-Konfiguration ungültig — nutze Defaults")
            self._validity_hours = DEFAULT_PREAUTH_VALIDITY_HOURS
            self._max_duration_hours = DEFAULT_PREAUTH_MAX_HOURS

    async def _persist(self) -> None:
        await self._store.async_save(
            {
                "validity_hours": self._validity_hours,
                "max_duration_hours": self._max_duration_hours,
            }
        )

    # --------------------------------------------------------- Status (read-only)

    @property
    def status(self) -> str:
        """Abgeleiteter Status: idle | pre_authorized | session_active."""
        if self._session_obj is not None:
            return STATUS_SESSION_ACTIVE
        if self._pre_auth is not None and self._pre_auth.is_active():
            return STATUS_PRE_AUTHORIZED
        return STATUS_IDLE

    @property
    def is_pre_authorized(self) -> bool:
        return self._pre_auth is not None and self._pre_auth.is_active()

    @property
    def pre_authorization(self) -> PreAuthorization | None:
        # Lazy-Cleanup bei abgelaufener Pre-Auth
        if self._pre_auth and not self._pre_auth.is_active():
            self._pre_auth = None
            self._publish_state()
        return self._pre_auth

    @property
    def session(self) -> ActiveSession | None:
        return self._session_obj

    # --------------------------------------------------------- Konfiguration

    @property
    def validity_hours(self) -> float:
        return self._validity_hours

    @property
    def max_duration_hours(self) -> int:
        return self._max_duration_hours

    async def set_validity_hours(self, hours: float) -> None:
        hours = max(1.0, min(float(hours), float(MAX_PREAUTH_VALIDITY_HOURS)))
        if hours == self._validity_hours:
            return
        self._validity_hours = hours
        await self._persist()
        self._publish_state()

    async def set_max_duration_hours(self, hours: int) -> None:
        hours = max(1, min(int(hours), MAX_SESSION_HOURS))
        if hours == self._max_duration_hours:
            return
        self._max_duration_hours = hours
        await self._persist()
        self._publish_state()

    # --------------------------------------------------------- Vorab-Freigabe

    async def grant_pre_authorization(
        self,
        expires_in_hours: float | None = None,
        max_duration_hours: int | None = None,
    ) -> PreAuthorization:
        """Vorab-Freigabe erteilen — nutzt persistierte Defaults, wenn keine
        Parameter mitgegeben werden."""
        validity = float(expires_in_hours) if expires_in_hours is not None else self._validity_hours
        validity = max(0.1, min(validity, float(MAX_PREAUTH_VALIDITY_HOURS)))

        max_hours = max(
            1,
            min(
                int(max_duration_hours) if max_duration_hours is not None else self._max_duration_hours,
                MAX_SESSION_HOURS,
            ),
        )

        self._pre_auth = PreAuthorization(
            expires_at=dt_util.utcnow() + timedelta(hours=validity),
            max_duration_hours=max_hours,
        )

        if self._preauth_expire_cancel is not None:
            self._preauth_expire_cancel()
        self._preauth_expire_cancel = async_call_later(
            self._hass,
            validity * 3600,
            self._on_preauth_expired,
        )

        await self._announce_preauth()
        self._publish_state()
        _LOGGER.info(
            "Vorab-Freigabe erteilt — gültig bis %s, max. %d h",
            self._pre_auth.expires_at.isoformat(),
            max_hours,
        )
        return self._pre_auth

    async def async_end_session(self, *, reason: str = "manual") -> bool:
        """Beendet die laufende Wartungs-Session.

        Wird vom TunnelForwarder beim manuellen Tunnel-Trennen gerufen
        (REQUIREMENTS §4.4 — Endkunden-Abbruch). Gibt True zurück,
        wenn tatsächlich eine Session aktiv war.
        """
        if self._session_obj is None:
            return False
        await self._end_session(reason=reason)
        return True

    async def revoke_pre_authorization(self) -> None:
        """Widerruft die Vorab-Freigabe (§4.3)."""
        if self._pre_auth is None and self._preauth_expire_cancel is None:
            return
        self._pre_auth = None
        if self._preauth_expire_cancel is not None:
            self._preauth_expire_cancel()
            self._preauth_expire_cancel = None
        await self._announce_preauth()
        self._publish_state()
        _LOGGER.info("Vorab-Freigabe widerrufen")

    @callback
    def _on_preauth_expired(self, _now: Any) -> None:
        self._pre_auth = None
        self._preauth_expire_cancel = None
        self._publish_state()
        self._hass.async_create_task(self._announce_preauth())
        _LOGGER.info("Vorab-Freigabe ist abgelaufen")

    async def _announce_preauth(self) -> None:
        """Meldet den aktuellen Pre-Auth-Status per REST ans Backend."""
        headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=10)

        if self._pre_auth and self._pre_auth.is_active():
            # Pre-Auth setzen: POST /api/agent/preauth
            url = f"{self._backend_url}/api/agent/preauth"
            body = self._pre_auth.to_dict()
            try:
                async with self._session.post(
                    url, json=body, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status not in (200, 201, 204):
                        _LOGGER.warning(
                            "Pre-Auth POST fehlgeschlagen (HTTP %d)", resp.status
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Pre-Auth POST Fehler: %s", err)
        else:
            # Pre-Auth widerrufen: DELETE /api/agent/preauth
            url = f"{self._backend_url}/api/agent/preauth"
            try:
                async with self._session.delete(
                    url, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status not in (200, 204, 404):
                        # 404 ist akzeptabel — kein Pre-Auth vorhanden
                        _LOGGER.warning(
                            "Pre-Auth DELETE fehlgeschlagen (HTTP %d)", resp.status
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.warning("Pre-Auth DELETE Fehler: %s", err)

    # --------------------------------------------------------- Connection-Request

    async def _on_connection_request(self, data: dict[str, Any]) -> None:
        """Verbindungsanfrage vom Integrator empfangen (§4.2 / §4.3).

        Wird vom RequestPoller aufgerufen (action="connection_request").

        Backend (Quarkus) serialisiert camelCase: requestId, duration.
        snake_case-Fallbacks bleiben für Backwards-Kompatibilität und Tests.
        """
        request_id = data.get("requestId") or data.get("request_id") or ""
        subject = data.get("subject") or ""
        reason = data.get("reason") or ""
        duration_hours = self._coerce_duration(
            data.get("duration") if data.get("duration") is not None else data.get("duration_hours")
        )

        if not request_id:
            _LOGGER.warning(
                "connection_request ohne requestId empfangen — ignoriert: %s", data
            )
            return

        # Self-Healing (#90): Lag ein Repair-Issue fuer eine ANDERE Anfrage offen,
        # ist diese inzwischen erledigt (vom Integrator abgebrochen oder abgelaufen) —
        # der Poll wuerde sie nie wieder melden. Altes Issue jetzt entfernen.
        if self._open_request_id and self._open_request_id != request_id:
            await self._dismiss_notification(self._open_request_id)

        if self._pre_auth and self._pre_auth.is_active():
            # §4.3 — Vorab-Freigabe: automatisch genehmigen, Dauer kappen.
            # Ein evtl. fuer genau diese Anfrage offenes Issue wird hinfaellig —
            # der Auto-Accept ersetzt die manuelle Endkunden-Entscheidung (#90).
            await self._dismiss_notification(request_id)
            duration_hours = min(duration_hours, self._pre_auth.max_duration_hours)
            await self._accept(request_id, subject, reason, duration_hours, auto=True)
            return

        # §4.2 — Standardablauf: persistente Notification erzeugen
        await self._notify_user(request_id, subject, reason, duration_hours)

    async def confirm_request(
        self, request_id: str, accepted: bool, duration_hours: int | None = None
    ) -> None:
        """Endkunden-Service: bestätigt oder lehnt eine wartende Anfrage ab."""
        if not request_id:
            return
        if not accepted:
            await self._post_response(request_id, accepted=False)
            await self._dismiss_notification(request_id)
            return

        duration = self._coerce_duration(duration_hours)
        await self._accept(request_id, "", "", duration, auto=False)
        await self._dismiss_notification(request_id)

    async def _accept(
        self,
        request_id: str,
        subject: str,
        reason: str,
        duration_hours: int,
        *,
        auto: bool,
    ) -> None:
        """Anfrage annehmen: Wartungs-User scharf schalten, REST-Accept, Session starten.

        Fail-Closed (#110): Der Wartungs-User wird VOR dem Accept aktiviert (und
        sein Passwort rotiert). Schlaegt das fehl (z.B. User vom Endkunden
        geloescht), wird die Anfrage abgelehnt statt eine Session ohne
        funktionierenden Login zu eroeffnen.
        """
        if self._integrator_user is not None:
            creds = await self._integrator_user.async_activate()
            if creds is None or creds.error:
                _LOGGER.error(
                    "Wartungs-User nicht aktivierbar (%s) — Anfrage %s wird abgelehnt",
                    creds.error if creds else "keine Credentials",
                    request_id,
                )
                await self._post_response(request_id, accepted=False)
                await self._dismiss_notification(request_id)
                return

        await self._post_response(
            request_id, accepted=True, duration_hours=duration_hours
        )
        await self._start_session(request_id, subject, reason, duration_hours)

    async def _post_response(
        self,
        request_id: str,
        *,
        accepted: bool,
        duration_hours: int | None = None,
    ) -> None:
        """Sendet Accept oder Reject per REST an das Backend."""
        action = "accept" if accepted else "reject"
        url = (
            f"{self._backend_url}/api/agent/connection-requests"
            f"/{request_id}/{action}"
        )
        headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {}
        if accepted and duration_hours is not None:
            body["duration_hours"] = duration_hours

        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._session.post(
                url, json=body, headers=headers, timeout=timeout
            ) as resp:
                if resp.status not in (200, 201, 204):
                    _LOGGER.warning(
                        "Connection-Request %s fehlgeschlagen (HTTP %d)",
                        action,
                        resp.status,
                    )
                else:
                    _LOGGER.info(
                        "Connection-Request %s: %s (HTTP %d)",
                        request_id,
                        action,
                        resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Connection-Request %s Fehler: %s", action, err
            )

    # --------------------------------------------------------- Session

    async def _start_session(
        self, request_id: str, subject: str, reason: str, duration_hours: int
    ) -> None:
        """Eröffnet das Wartungsfenster — Auto-Disable nach `duration_hours`."""
        # User NICHT deaktivieren: _accept hat ihn gerade aktiviert; ein evtl.
        # laufender Vorgaenger wird nur abgeloest (Handover/Neustart), nicht beendet.
        await self._end_session(reason="restart", emit=False, deactivate_user=False)

        self._session_obj = ActiveSession(
            request_id=request_id,
            subject=subject,
            reason=reason,
            duration_hours=duration_hours,
        )

        self._session_expire_cancel = async_call_later(
            self._hass,
            duration_hours * 3600,
            self._on_session_expired,
        )
        self._publish_state()
        _LOGGER.info(
            "Wartungsfenster gestartet (request=%s, dauer=%d h)",
            request_id,
            duration_hours,
        )

    @callback
    def _on_session_expired(self, _now: Any) -> None:
        self._hass.async_create_task(self._end_session(reason="timeout"))

    async def _end_session(
        self, *, reason: str, emit: bool = True, deactivate_user: bool = True
    ) -> None:
        if self._session_obj is None and self._session_expire_cancel is None:
            return
        if self._session_expire_cancel is not None:
            self._session_expire_cancel()
            self._session_expire_cancel = None
        if self._session_obj is not None:
            _LOGGER.info(
                "Wartungsfenster beendet (request=%s, grund=%s)",
                self._session_obj.request_id,
                reason,
            )
            self._session_obj = None
        # Wartungs-User wieder fail-closed deaktivieren (#110, + Refresh-Token-Kill).
        # Beim internen Neustart (reason="restart" aus _start_session) NICHT — dort
        # wird unmittelbar eine neue Session mit frisch aktiviertem User eroeffnet.
        if deactivate_user and self._integrator_user is not None:
            await self._integrator_user.async_deactivate()
        if emit:
            self._publish_state()

    # --------------------------------------------------------- Notifications

    async def _notify_user(
        self, request_id: str, subject: str, reason: str, duration_hours: int
    ) -> None:
        """Legt ein Repair-Issue an — Endkunde sieht es als gelben Banner
        auf dem HA-Dashboard und kann den Repair-Flow starten."""
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._issue_id(request_id),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="connection_request",
            translation_placeholders={
                "subject": subject or "-",
                "reason": reason or "-",
                "requested_hours": str(duration_hours),
            },
            data={
                "entry_id": self._entry_id,
                "request_id": request_id,
                "subject": subject,
                "reason": reason,
                "duration_hours": duration_hours,
            },
        )
        self._open_request_id = request_id

    async def _dismiss_notification(self, request_id: str) -> None:
        try:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(request_id))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Konnte Issue nicht entfernen", exc_info=True)
        if self._open_request_id == request_id:
            self._open_request_id = None

    async def _on_poll_idle(self, _data: dict[str, Any] | None = None) -> None:
        """Poll meldete 'nichts offen' (HTTP 204) → verwaistes Repair-Issue entfernen (#90).

        Greift bei Integrator-Abbruch UND Ablauf: sobald die Anfrage im Backend
        nicht mehr PENDING ist, liefert der Poll 204. Hat der Endkunde bis dahin
        nicht selbst entschieden, steht sein Repair-Issue noch — es wird hier
        aufgeraeumt. Wird zusaetzlich defensiv beim Tunnel-Aufbau
        (connection_accepted) aufgerufen, falls eine Vorab-Freigabe die Anfrage
        ohne Klick akzeptiert hat.
        """
        if self._open_request_id is not None:
            _LOGGER.info(
                "Keine offene Anfrage mehr — verwaistes Repair-Issue (request=%s) entfernt",
                self._open_request_id,
            )
            await self._dismiss_notification(self._open_request_id)

    @staticmethod
    def _issue_id(request_id: str) -> str:
        return f"{ISSUE_ID_PREFIX}{request_id}"

    # --------------------------------------------------------- Helpers

    @staticmethod
    def _coerce_duration(value: Any) -> int:
        try:
            hours = int(value)
        except (TypeError, ValueError):
            hours = MAX_SESSION_HOURS
        return max(1, min(hours, MAX_SESSION_HOURS))

    def _publish_state(self) -> None:
        async_dispatcher_send(
            self._hass,
            SIGNAL_REMOTE_ACCESS_STATE,
            self._entry_id,
            {
                "status": self.status,
                "pre_authorization": self._pre_auth.to_dict() if self._pre_auth else None,
                "session": (
                    {
                        "request_id": self._session_obj.request_id,
                        "subject": self._session_obj.subject,
                        "duration_hours": self._session_obj.duration_hours,
                        "started_at": self._session_obj.started_at.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "ends_at": self._session_obj.ends_at().isoformat().replace(
                            "+00:00", "Z"
                        ),
                    }
                    if self._session_obj
                    else None
                ),
                "validity_hours": self._validity_hours,
                "max_duration_hours": self._max_duration_hours,
            },
        )

    async def async_shutdown(self) -> None:
        if self._session_expire_cancel is not None:
            self._session_expire_cancel()
            self._session_expire_cancel = None
        if self._preauth_expire_cancel is not None:
            self._preauth_expire_cancel()
            self._preauth_expire_cancel = None
        self._session_obj = None
        self._pre_auth = None
        self._open_request_id = None
