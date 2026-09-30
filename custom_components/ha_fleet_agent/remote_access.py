"""Fernzugriffs-Logik: Vorab-Freigabe + Verbindungsanfrage-Bestätigung.

Modellierung gemäss REQUIREMENTS §4:

- §4.2 — Standardablauf: Integrator stellt Anfrage (Betreff, Dauer, Grund) →
  Endkunde sieht alles in einer Persistent Notification und bestätigt über
  den Service `ha_fleet_agent.confirm_request`.
- §4.3 — Vorab-Freigabe: Endkunde legt im Voraus ein Zeitfenster (Gültigkeit)
  und eine maximale Sessiondauer fest. Innerhalb des Fensters genehmigt der
  Agent eingehende Anfragen automatisch und kappt deren Dauer auf das Maximum.
  Seit #167 auch ohne Ablaufdatum (bis zum Widerruf); jede einzelne Sitzung
  bleibt befristet. Dann erinnert das Plugin alle 30 Tage an die Freigabe und
  gleicht sie über das Poll-Feld ``preauth`` mit dem Backend ab.

Phase 4 (TODO #50.23): Connection-Response + Preauth-Announce per REST statt
WebSocket-Frame. Alle Backend-Calls nutzen die übergebene aiohttp.ClientSession.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, NamedTuple

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import preauth_reminder
from .const import (
    DEFAULT_LANGUAGE,
    DEFAULT_PREAUTH_MAX_HOURS,
    DEFAULT_PREAUTH_VALIDITY_HOURS,
    DOMAIN,
    ISSUE_ID_PREFIX,
    MAX_PREAUTH_VALIDITY_HOURS,
    MAX_REMEMBERED_ENDED_REQUESTS,
    MAX_SESSION_HOURS,
    PREAUTH_REMINDER_CHECK_HOURS,
    PREAUTH_REMINDER_INTERVAL_DAYS,
    PREAUTH_SYNC_BACKOFF_MAX_MINUTES,
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
    """Vom Endkunden vorab erteilte Freigabe (§4.3).

    Befristet: ``expires_at`` gesetzt. Dauerhaft (#167): ``unlimited`` und kein
    ``expires_at`` — gilt bis zum Widerruf. Befristet bleibt dann jede einzelne
    Sitzung (``max_duration_hours``, ``MAX_SESSION_HOURS``).
    """

    expires_at: datetime | None
    max_duration_hours: int
    unlimited: bool = False

    def is_active(self, now: datetime | None = None) -> bool:
        if self.unlimited:
            return True
        if self.expires_at is None:
            # Fail-closed: ohne Ablauf gilt eine Freigabe nur mit Kennzeichen.
            return False
        return (now or dt_util.utcnow()) < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Store-Format und zugleich Body von ``POST /api/agent/preauth``."""
        return {
            "expires_at": _iso(self.expires_at) if self.expires_at is not None else None,
            "max_duration_hours": self.max_duration_hours,
            "unlimited": self.unlimited,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PreAuthorization | None:
        """Gegenstück zu ``to_dict`` für den Store; ``None`` bei unbrauchbaren Daten.

        Fail-closed (#167): Dauerhaft nur mit ``"unlimited": true`` **und**
        ausdrücklich ``"expires_at": null``. Fehlt ``expires_at``, ist es unlesbar
        oder fehlt das Kennzeichen, wird die Freigabe verworfen — eine dauerhafte
        Freigabe entsteht nie aus kaputten oder fehlenden Daten.
        """
        if not isinstance(data, dict):
            return None
        try:
            max_hours = int(data.get("max_duration_hours"))
        except (TypeError, ValueError):
            return None
        if data.get("unlimited") is True:
            if "expires_at" not in data or data["expires_at"] is not None:
                return None
            return cls(expires_at=None, max_duration_hours=max_hours, unlimited=True)
        expires_at = parse_iso_utc(data.get("expires_at"))
        if expires_at is None:
            return None
        return cls(expires_at=expires_at, max_duration_hours=max_hours)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def parse_iso_utc(value: Any) -> datetime | None:
    """ISO-8601 (auch mit ``Z``, Offset oder Nanosekunden) → aware datetime.

    ``None`` bei fehlendem oder unlesbarem Wert — so behandelt der Aufrufer ein
    älteres Backend ohne das Feld und kaputte Daten gleich (Store, Poll-Antwort).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class ActiveSession:
    """Eine aktive Fernzugriffs-Session."""

    request_id: str
    subject: str
    reason: str
    duration_hours: int
    started_at: datetime = field(default_factory=dt_util.utcnow)
    # Serverseitiges Session-Ende, wenn die Session aus connection_accepted
    # eingerichtet wurde (Vorab-Freigabe, HA-Neustart). Hat Vorrang vor der Dauer.
    expires_at: datetime | None = None

    def ends_at(self) -> datetime:
        if self.expires_at is not None:
            return self.expires_at
        return self.started_at + timedelta(hours=self.duration_hours)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "subject": self.subject,
            "reason": self.reason,
            "duration_hours": self.duration_hours,
            "started_at": _iso(self.started_at),
            "expires_at": _iso(self.ends_at()),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ActiveSession | None:
        """Gegenstück zu ``to_dict`` für den Store; ``None`` bei unbrauchbaren Daten."""
        if not isinstance(data, dict) or not data.get("request_id"):
            return None
        started_at = parse_iso_utc(data.get("started_at"))
        expires_at = parse_iso_utc(data.get("expires_at"))
        if started_at is None or expires_at is None:
            return None
        try:
            duration_hours = int(data.get("duration_hours"))
        except (TypeError, ValueError):
            return None
        return cls(
            request_id=str(data["request_id"]),
            subject=str(data.get("subject") or ""),
            reason=str(data.get("reason") or ""),
            duration_hours=duration_hours,
            started_at=started_at,
            expires_at=expires_at,
        )


_ORIGIN_RESUME = "Fortsetzung nach Neustart"


class _LocalRelease(NamedTuple):
    """Lokale Freigabe für ein connection_accepted ohne laufende Session (#165)."""

    cap: datetime  # spätestes Session-Ende laut lokaler Freigabe
    subject: str
    reason: str
    origin: str  # nur fürs Log


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
        language: str = DEFAULT_LANGUAGE,
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

        # Lebenszyklus einer Anfrage im Plugin (#165). Invarianten:
        # - `_session_obj` und `_resumable` schließen sich aus: fortsetzbar ist eine
        #   Session nur, solange sie nach einem Neustart noch nicht wieder läuft.
        # - Eine requestId in `_ended_request_ids` bekommt nie wieder eine Session.
        # - `_server_expiry` enthält nur Anfragen, die auf die Bestätigung des
        #   Endkunden warten (offenes Repair-Issue); Annehmen, Beenden und Aufräumen
        #   des Issues entfernen den Eintrag.
        # - `_confirmed` enthält nur bestätigte, noch nicht gestartete Anfragen; der
        #   Session-Start und das Beenden entfernen den Eintrag.
        self._pre_auth: PreAuthorization | None = None
        self._session_obj: ActiveSession | None = None
        # Vor einem HA-Neustart laufende Session (#165): aus dem Store geladen, aber
        # noch nicht aktiv — der Wartungs-User ruht nach dem Setup fail-closed. Erst
        # ein connection_accepted für genau diese Anfrage setzt sie ohne Rückfrage fort.
        self._resumable: ActiveSession | None = None
        # Vom Plugin beendete Anfragen (Endkunde trennt, Ablauf, Reconnect-Aufgabe,
        # Ablehnung). Ein spätes connection_accepted für sie belebt NIE eine Session
        # wieder, sondern holt das Schließen im Backend nach (#165). Persistiert.
        self._ended_request_ids: list[str] = []
        # Serverseitiges Session-Ende je Anfrage, die auf die Bestätigung des
        # Endkunden wartet — kappt die im Repair-Flow gewählte Dauer.
        self._server_expiry: dict[str, datetime] = {}
        # Vom Endkunden bestätigte Anfragen, die das Backend schon angenommen hatte:
        # requestId → spätestes Session-Ende. Die Session entsteht erst beim nächsten
        # connection_accepted — das kommt nur für ACCEPTED, ein 409 beim Accept-POST
        # unterschiede ACCEPTED nicht von inzwischen CLOSED/CANCELLED (#165).
        self._confirmed: dict[str, datetime] = {}
        # Gesetzt beim Unload: Ein noch laufender Poll-Handler darf danach keine
        # Session mehr anlegen und persistieren (#165).
        self._shutting_down = False
        # Meldungen, die sich im 15-s-Poll-Takt wiederholen würden, nur einmal je
        # Anfrage auf INFO/WARNING loggen (Schlüssel: "<art>:<requestId>").
        self._logged_once: set[str] = set()
        # Schließt beim Session-Ende den Tunnel (#165). Sonst bliebe er nach einem
        # Ablauf im Plugin offen, bis der Backend-Sweep ihn fände — der sieht die per
        # close geschlossene Anfrage aber nicht mehr.
        self._on_session_end: Callable[[], Awaitable[Any]] | None = None

        # request_id des aktuell offenen Repair-Issues (#90). Grundlage fuer das
        # Self-Healing: verwaiste Issues (Integrator-Abbruch/Ablauf) werden beim
        # naechsten Poll entfernt, sobald das Backend "nichts offen" meldet.
        self._open_request_id: str | None = None

        # Vom Endkunden konfigurierte Defaults — werden persistiert. Wie die beiden
        # Numbers gilt der Switch „Gültigkeit ohne Ablaufdatum" erst für das nächste Erteilen (#167).
        self._validity_hours: float = DEFAULT_PREAUTH_VALIDITY_HOURS
        self._max_duration_hours: int = DEFAULT_PREAUTH_MAX_HOURS
        self._unlimited: bool = False

        # Erinnerung an eine dauerhafte Vorab-Freigabe (#167): Zeitpunkt des letzten
        # Hinweises bzw. des Erteilens. Persistiert, damit ein Neustart weder eine
        # Erinnerung auslöst noch eine verschluckt.
        self._language = language
        self._last_reminder_at: datetime | None = None
        self._reminder_unsub: Callable[[], None] | None = None

        # Abgleich lokal ↔ Backend (#167): Versuche seit der letzten Übereinstimmung
        # und frühester Zeitpunkt des nächsten Versuchs (Backoff).
        self._sync_attempts = 0
        self._sync_retry_at: datetime | None = None
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}"
        )

        # Cancel-Handles für ausstehende Auto-Timer
        self._session_expire_cancel = None
        self._preauth_expire_cancel = None

    # --------------------------------------------------------- Setup

    def set_session_end_callback(self, callback: Callable[[], Awaitable[Any]]) -> None:
        """Setzt den Callback, der beim Session-Ende den Tunnel schließt."""
        self._on_session_end = callback

    async def async_load(self) -> None:
        """Lädt Konfiguration, Vorab-Freigabe, fortsetzbare Session und beendete Anfragen.

        Vorab-Freigabe und Session überleben seit #165 einen HA-Neustart: Ohne sie
        wüsste das Plugin nach dem Neustart nicht, ob ein connection_accepted vom
        Endkunden freigegeben ist, und müsste dem Backend blind vertrauen.
        """
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
        # Nur ein ausdrückliches true schaltet „Gültigkeit ohne Ablaufdatum" ein (fail-closed).
        self._unlimited = data.get("unlimited") is True
        self._last_reminder_at = parse_iso_utc(data.get("last_reminder_at"))

        now = dt_util.utcnow()
        pre_auth = PreAuthorization.from_dict(data.get("pre_authorization"))
        if pre_auth is not None and pre_auth.is_active(now):
            self._pre_auth = pre_auth
            if pre_auth.expires_at is not None:
                self._preauth_expire_cancel = async_call_later(
                    self._hass,
                    (pre_auth.expires_at - now).total_seconds(),
                    self._on_preauth_expired,
                )
        ended = data.get("ended_request_ids")
        if isinstance(ended, list):
            self._ended_request_ids = [str(r) for r in ended if r][
                -MAX_REMEMBERED_ENDED_REQUESTS:
            ]
        session = ActiveSession.from_dict(data.get("session"))
        if session is not None:
            self._resumable = session
            if session.ends_at() <= now:
                # Während HA aus war abgelaufen: ohne reguläres Session-Ende, also
                # ohne Token-Kill — jetzt nachholen und beenden (#165).
                await self._discard_resumable()

    async def _persist(self) -> None:
        session = self._session_obj or self._resumable
        await self._store.async_save(
            {
                "validity_hours": self._validity_hours,
                "max_duration_hours": self._max_duration_hours,
                "unlimited": self._unlimited,
                "last_reminder_at": (
                    _iso(self._last_reminder_at) if self._last_reminder_at else None
                ),
                "pre_authorization": (
                    self._pre_auth.to_dict()
                    if self._pre_auth and self._pre_auth.is_active()
                    else None
                ),
                "session": session.to_dict() if session else None,
                "ended_request_ids": self._ended_request_ids,
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

    @property
    def unlimited(self) -> bool:
        """Switch „Gültigkeit ohne Ablaufdatum": gilt für das nächste Erteilen, nicht die laufende Freigabe."""
        return self._unlimited

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

    async def set_unlimited(self, unlimited: bool) -> None:
        """Switch „Gültigkeit ohne Ablaufdatum" (#167) — wie die Numbers nur für das nächste Erteilen."""
        unlimited = bool(unlimited)
        if unlimited == self._unlimited:
            return
        self._unlimited = unlimited
        await self._persist()
        self._publish_state()

    # --------------------------------------------------------- Vorab-Freigabe

    async def grant_pre_authorization(
        self,
        expires_in_hours: float | None = None,
        max_duration_hours: int | None = None,
        unlimited: bool | None = None,
    ) -> PreAuthorization:
        """Vorab-Freigabe erteilen — nutzt persistierte Defaults, wenn keine
        Parameter mitgegeben werden.

        ``unlimited`` (#167): Freigabe ohne Ablaufdatum, gilt bis zum Widerruf —
        ohne Ablauf-Timer, stattdessen mit Erinnerung alle 30 Tage. Die
        Gültigkeitsdauer wird dann ignoriert, die maximale Sitzungsdauer nicht.
        """
        unlimited = self._unlimited if unlimited is None else bool(unlimited)
        max_hours = max(
            1,
            min(
                int(max_duration_hours) if max_duration_hours is not None else self._max_duration_hours,
                MAX_SESSION_HOURS,
            ),
        )
        now = dt_util.utcnow()

        if self._preauth_expire_cancel is not None:
            self._preauth_expire_cancel()
            self._preauth_expire_cancel = None

        if unlimited:
            self._pre_auth = PreAuthorization(
                expires_at=None, max_duration_hours=max_hours, unlimited=True
            )
            # Die erste Erinnerung kommt 30 Tage nach dem Erteilen; erneutes Erteilen
            # setzt den Zähler zurück.
            self._last_reminder_at = now
        else:
            validity = float(expires_in_hours) if expires_in_hours is not None else self._validity_hours
            validity = max(0.1, min(validity, float(MAX_PREAUTH_VALIDITY_HOURS)))
            self._pre_auth = PreAuthorization(
                expires_at=now + timedelta(hours=validity),
                max_duration_hours=max_hours,
            )
            self._preauth_expire_cancel = async_call_later(
                self._hass,
                validity * 3600,
                self._on_preauth_expired,
            )
            self._last_reminder_at = None
            preauth_reminder.async_dismiss(self._hass, self._entry_id)

        # Erst lokal festschreiben, dann melden: Die persistierte Freigabe ist
        # maßgeblich (#165), der Netzwerk-Call kann bis zu 10 s dauern.
        await self._persist()
        await self._announce_preauth()
        self._publish_state()
        _LOGGER.info(
            "Vorab-Freigabe erteilt — gültig bis %s, max. %d h",
            "auf Widerruf" if unlimited else _iso(self._pre_auth.expires_at),
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
        self._last_reminder_at = None
        preauth_reminder.async_dismiss(self._hass, self._entry_id)
        if self._preauth_expire_cancel is not None:
            self._preauth_expire_cancel()
            self._preauth_expire_cancel = None
        # Erst lokal festschreiben: Startet HA neu, während der DELETE noch hängt,
        # darf async_load die widerrufene Freigabe nicht wiederherstellen (#165).
        await self._persist()
        await self._announce_preauth()
        self._publish_state()
        _LOGGER.info("Vorab-Freigabe widerrufen")

    @callback
    def _on_preauth_expired(self, _now: Any) -> None:
        self._pre_auth = None
        self._preauth_expire_cancel = None
        self._publish_state()
        self._hass.async_create_task(self._announce_preauth())
        self._hass.async_create_task(self._persist())
        _LOGGER.info("Vorab-Freigabe ist abgelaufen")

    async def _announce_preauth(self, *, log_failure: bool = True) -> bool:
        """Meldet den aktuellen Pre-Auth-Status per REST ans Backend.

        Rückgabe ``True``, wenn das Backend die Meldung angenommen hat. Der
        Abgleich (#167) loggt Fehlschläge selbst (einmal je Abweichung) und ruft
        deshalb mit ``log_failure=False``.
        """
        headers = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=10)
        url = f"{self._backend_url}/api/agent/preauth"
        log = _LOGGER.warning if log_failure else _LOGGER.debug

        if self._pre_auth and self._pre_auth.is_active():
            # Pre-Auth setzen: POST /api/agent/preauth
            body = self._pre_auth.to_dict()
            try:
                async with self._session.post(
                    url, json=body, headers=headers, timeout=timeout
                ) as resp:
                    if resp.status not in (200, 201, 204):
                        log("Pre-Auth POST fehlgeschlagen (HTTP %d)", resp.status)
                        return False
                    return True
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                log("Pre-Auth POST Fehler: %s", err)
                return False
        # Pre-Auth widerrufen: DELETE /api/agent/preauth
        try:
            async with self._session.delete(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status not in (200, 204, 404):
                    # 404 ist akzeptabel — kein Pre-Auth vorhanden
                    log("Pre-Auth DELETE fehlgeschlagen (HTTP %d)", resp.status)
                    return False
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            log("Pre-Auth DELETE Fehler: %s", err)
            return False

    # --------------------------------------------------------- Abgleich (#167)

    async def async_on_poll_response(self, data: dict[str, Any]) -> None:
        """Listener für jede Poll-Antwort mit Body. Fehlt ``preauth``, ist das
        Backend älter als #167 — dann gibt es nichts abzugleichen."""
        if "preauth" in data:
            await self.async_reconcile_preauth(data["preauth"])

    async def async_reconcile_preauth(self, backend: Any) -> None:
        """Gleicht die lokale Vorab-Freigabe mit der Backend-Sicht aus dem Poll ab.

        Das Plugin ist maßgeblich, das Backend Spiegel (§4.3). Bisher hat die
        Befristung jede Abweichung nach spätestens 168 h geheilt; eine dauerhafte
        Freigabe tut das nicht mehr. Deshalb vergleicht das Plugin nach jedem Poll
        (``backend`` = Poll-Feld ``preauth``: ``None`` oder
        ``{"expiresAt", "maxDurationH", "unlimited"}``) und meldet bei einer
        Abweichung seinen Stand erneut (POST bzw. DELETE).

        Backoff: Der erste Versuch läuft sofort. Besteht die Abweichung danach
        weiter — Backend nicht erreichbar, HTTP 4xx (neues Plugin, altes Backend)
        oder eine Meldung, die dort nicht ankommt —, wartet das Plugin 1, 2, 4 …
        höchstens 60 Minuten und loggt das nur einmal.
        """
        if self._shutting_down:
            return
        now = dt_util.utcnow()
        matches = self._matches_backend(backend, now)
        if matches is None:
            return
        if matches:
            if self._sync_attempts:
                _LOGGER.info("Vorab-Freigabe mit dem Backend abgeglichen")
            self._sync_attempts = 0
            self._sync_retry_at = None
            self._logged_once.discard("preauth-sync")
            return
        if self._sync_retry_at is not None and now < self._sync_retry_at:
            return

        if self._sync_attempts and self._log_once("preauth-sync"):
            _LOGGER.warning(
                "Vorab-Freigabe weicht weiter vom Backend ab — Abgleich wird mit "
                "wachsendem Abstand (bis %d min) wiederholt, weitere Versuche nur im Debug-Log",
                PREAUTH_SYNC_BACKOFF_MAX_MINUTES,
            )
        self._sync_attempts += 1
        delay_min = min(PREAUTH_SYNC_BACKOFF_MAX_MINUTES, 2 ** (self._sync_attempts - 1))
        self._sync_retry_at = now + timedelta(minutes=delay_min)
        ok = await self._announce_preauth(log_failure=False)
        _LOGGER.debug(
            "Abgleich der Vorab-Freigabe: %s gemeldet (%s), nächster Versuch frühestens in %d min",
            "Freigabe" if self.is_pre_authorized else "Widerruf",
            "angenommen" if ok else "fehlgeschlagen",
            delay_min,
        )

    def _matches_backend(self, backend: Any, now: datetime) -> bool | None:
        """Stimmt die Backend-Sicht mit der lokalen Freigabe überein?

        Verglichen werden aktiv ja/nein, Maximaldauer, dauerhaft ja/nein und bei
        befristeten Freigaben der Ablauf auf die Sekunde — so vergleicht auch das
        Backend beim idempotenten Anlegen. ``None`` = Backend-Sicht unlesbar: dann
        nicht handeln, statt auf kaputte Daten hin zu melden oder zu widerrufen.
        """
        local = self._pre_auth if self._pre_auth and self._pre_auth.is_active(now) else None
        if backend is None:
            return local is None
        if not isinstance(backend, dict):
            _LOGGER.debug("Poll-Feld preauth unlesbar — kein Abgleich: %r", backend)
            return None
        if local is None:
            return False
        try:
            backend_max = int(backend.get("maxDurationH"))
        except (TypeError, ValueError):
            _LOGGER.debug("Poll-Feld preauth ohne maxDurationH — kein Abgleich: %r", backend)
            return None
        backend_unlimited = backend.get("unlimited") is True
        if backend_max != local.max_duration_hours or backend_unlimited != local.unlimited:
            return False
        if local.unlimited:
            return True
        backend_expires = parse_iso_utc(backend.get("expiresAt"))
        return (
            backend_expires is not None
            and local.expires_at is not None
            and backend_expires.replace(microsecond=0) == local.expires_at.replace(microsecond=0)
        )

    # --------------------------------------------------------- Erinnerung (#167)

    def async_start_reminder(self) -> None:
        """Startet den täglichen Check und prüft einmal sofort (nach dem Laden)."""
        if self._reminder_unsub is None:
            self._reminder_unsub = async_track_time_interval(
                self._hass,
                self._on_reminder_tick,
                timedelta(hours=PREAUTH_REMINDER_CHECK_HOURS),
            )
        self._hass.async_create_task(self.async_check_reminder())

    async def _on_reminder_tick(self, _now: Any) -> None:
        await self.async_check_reminder()

    async def async_check_reminder(self) -> None:
        """Erinnert an eine dauerhafte Vorab-Freigabe, wenn der letzte Hinweis
        mindestens 30 Tage zurückliegt. ``last_reminder_at`` wird persistiert."""
        pre_auth = self._pre_auth
        if pre_auth is None or not pre_auth.unlimited or self._shutting_down:
            return
        now = dt_util.utcnow()
        if self._last_reminder_at is None:
            # Kein Bezugspunkt (Store von Hand bearbeitet): ab jetzt zählen, statt
            # sofort zu erinnern oder die Erinnerung ganz zu verlieren.
            self._last_reminder_at = now
            await self._persist()
            return
        if now - self._last_reminder_at < timedelta(days=PREAUTH_REMINDER_INTERVAL_DAYS):
            return
        preauth_reminder.async_show(
            self._hass, self._entry_id, self._language, pre_auth.max_duration_hours
        )
        self._last_reminder_at = now
        await self._persist()
        _LOGGER.info("Erinnerung an die dauerhafte Vorab-Freigabe angezeigt")

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

        if request_id in self._ended_request_ids:
            # Abgelehnt, aber der Reject kam nicht an (#165): nie annehmen — auch nicht
            # per Vorab-Freigabe —, sondern die Ablehnung wiederholen.
            if self._log_once(f"ended-pending:{request_id}"):
                _LOGGER.info(
                    "Verbindungsanfrage %s wurde bereits abgelehnt — Ablehnung wird wiederholt",
                    request_id,
                )
            await self._post_response(request_id, accepted=False)
            return

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
        running = self._session_obj is not None and self._session_obj.request_id == request_id
        if not accepted:
            await self._post_response(request_id, accepted=False)
            await self._dismiss_notification(request_id)
            if running:
                # Ablehnen der laufenden Anfrage (veralteter Dialog, Service-Aufruf):
                # wirklich beenden — User deaktivieren, Tunnel schließen (#165).
                await self._end_session(reason="rejected")
            else:
                # Hat das Backend die Anfrage schon angenommen (Vorab-Freigabe im
                # Backend, die das Plugin nicht kennt), greift reject nicht mehr —
                # dann schließen, sonst kommt sie mit jedem Poll zurück (#165).
                await self._mark_ended(request_id)
            return

        if running or request_id in self._ended_request_ids:
            # Doppeltes Annehmen (Session läuft schon — kein zweites Aktivieren, das
            # rotierte das Passwort am laufenden Tunnel) oder Annehmen einer bereits
            # beendeten Anfrage (veralteter Dialog): nichts starten.
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
        if self._shutting_down or request_id in self._ended_request_ids:
            return
        now = dt_util.utcnow()
        server_expiry = self._server_expiry.pop(request_id, None)
        if server_expiry is not None:
            # Das Backend hat die Anfrage schon angenommen (Rückfrage nach
            # connection_accepted, #165): kein Accept-POST — der antwortete 409 und
            # unterschiede nicht zwischen ACCEPTED und inzwischen geschlossen. Nur die
            # Zustimmung merken; die Session entsteht beim nächsten connection_accepted.
            self._confirmed[request_id] = self._cap_expiry(
                now + timedelta(hours=duration_hours), server_expiry
            )
            _LOGGER.info(
                "Verbindungsanfrage %s bestätigt — Session startet mit dem nächsten Poll",
                request_id,
            )
            return

        if not await self._activate_user(request_id):
            await self._post_response(request_id, accepted=False)
            await self._dismiss_notification(request_id)
            await self._mark_ended(request_id)
            return

        status = await self._post_response(
            request_id, accepted=True, duration_hours=duration_hours
        )
        if status not in (200, 201, 204):
            # Fail-closed (#165): Ohne bestätigte Annahme keine Session. 409 = nicht
            # mehr offen (abgebrochen, abgelaufen, geschlossen) → beendet. Netzfehler
            # oder 5xx: Die Anfrage ist noch PENDING, der nächste Poll fragt erneut.
            _LOGGER.warning(
                "Verbindungsanfrage %s nicht angenommen (HTTP %s) — keine Session",
                request_id,
                status,
            )
            await self._deactivate_user()
            if status == 409:
                await self._dismiss_notification(request_id)
                await self._mark_ended(request_id)
            return
        if self._shutting_down or request_id in self._ended_request_ids:
            # Während der Awaits beendet (Ablehnung parallel, Unload).
            await self._deactivate_user()
            return
        await self._start_session(
            request_id,
            subject,
            reason,
            duration_hours,
            expires_at=self._cap_expiry(now + timedelta(hours=duration_hours), None),
        )

    async def _activate_user(self, request_id: str, *, resume: bool = False) -> bool:
        """Wartungs-User scharf schalten (#110). ``False`` = nicht aktivierbar (fail-closed).

        ``resume``: Fortsetzen derselben Session nach einem HA-Neustart — die
        Refresh-Tokens des noch offenen Integrator-Browsers bleiben erhalten.
        """
        if self._integrator_user is None:
            return True
        creds = await self._integrator_user.async_activate(remove_stale_tokens=not resume)
        if creds is not None and not creds.error:
            return True
        _LOGGER.error(
            "Wartungs-User nicht aktivierbar (%s) — Anfrage %s wird beendet",
            creds.error if creds else "keine Credentials",
            request_id,
        )
        return False

    async def _deactivate_user(self) -> None:
        if self._integrator_user is not None:
            await self._integrator_user.async_deactivate()

    async def _discard_resumable(self) -> None:
        """Fortsetzbare Session verwerfen: Sie endete ohne reguläres Session-Ende,
        also Wartungs-User samt Refresh-Tokens abräumen und die Anfrage beenden."""
        resumable, self._resumable = self._resumable, None
        if resumable is None:
            return
        await self._deactivate_user()
        await self._mark_ended(resumable.request_id)

    async def async_end_for_unload(self) -> None:
        """Unload (Reload, Deaktivieren, Entfernen): gewolltes Ende (#165).

        Beendet die laufende Session, verwirft eine noch fortsetzbare und sperrt
        weitere Session-Starts — ein Poll-Handler kann beim Unload noch laufen.
        """
        self._shutting_down = True
        await self.async_end_session(reason="unload")
        await self._discard_resumable()

    async def _post_response(
        self,
        request_id: str,
        *,
        accepted: bool,
        duration_hours: int | None = None,
    ) -> int | None:
        """Sendet Accept oder Reject per REST an das Backend.

        Rückgabe: HTTP-Status, ``None`` bei Netzwerkfehler. 409 (Anfrage nicht mehr
        offen) ist seit #165 ein erwarteter Fall — der Aufrufer entscheidet.
        """
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
                if resp.status == 409:
                    _LOGGER.debug(
                        "Connection-Request %s: %s — nicht mehr offen (HTTP 409)",
                        request_id,
                        action,
                    )
                elif resp.status not in (200, 201, 204):
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
                return resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Connection-Request %s Fehler: %s", action, err
            )
            return None

    async def _close_request(self, request_id: str) -> None:
        """POST /api/agent/connection-requests/{id}/close — Anfrage im Backend schließen.

        Idempotent: Das Backend ignoriert Anfragen, die nicht ACCEPTED sind. Ein
        Fehlschlag wird nur geloggt; die Anfrage bleibt in ``_ended_request_ids``,
        und das nächste connection_accepted für sie stößt das Schließen erneut an.
        """
        url = (
            f"{self._backend_url}/api/agent/connection-requests/{request_id}/close"
        )
        headers = {"X-API-Key": self._api_key}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with self._session.post(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status not in (200, 204, 404):
                    _LOGGER.warning(
                        "Anfrage %s schließen fehlgeschlagen (HTTP %d)",
                        request_id,
                        resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Anfrage %s schließen — Fehler: %s", request_id, err)

    async def _mark_ended(self, request_id: str) -> None:
        """Merkt eine vom Plugin beendete Anfrage (persistiert) und schließt sie im Backend."""
        self._server_expiry.pop(request_id, None)
        self._confirmed.pop(request_id, None)
        if request_id not in self._ended_request_ids:
            self._ended_request_ids.append(request_id)
            self._ended_request_ids = self._ended_request_ids[
                -MAX_REMEMBERED_ENDED_REQUESTS:
            ]
            await self._persist()
        await self._close_request(request_id)

    def _log_once(self, key: str) -> bool:
        """``True`` beim ersten Aufruf je Schlüssel — gegen Log-Spam im Poll-Takt."""
        if key in self._logged_once:
            return False
        self._logged_once.add(key)
        return True

    # --------------------------------------------------------- Session

    async def async_ensure_session_for_accepted(
        self,
        request_id: str,
        *,
        subject: str = "",
        session_expires_at: datetime | None = None,
    ) -> bool:
        """Entscheidet vor dem Tunnel-Aufbau, ob ``connection_accepted`` freigegeben ist.

        Das Backend meldet ``connection_accepted`` auch für Annahmen, die das Plugin
        nicht selbst ausgelöst hat (Vorab-Freigabe im Backend, HA-Neustart mitten in
        der Session). Früher blieb der Wartungs-User dann deaktiviert und der Tunnel
        stand ohne Zugangsdaten (Staging-Befund 2026-09-23). Das Plugin vertraut dem
        Backend dabei aber nicht blind (Entscheidung 2026-09-23, #165) — ohne
        Rückfrage freigeschaltet wird nur:

        1. die bereits laufende Session dieser Anfrage,
        2. die vor einem HA-Neustart laufende Session dieser Anfrage (persistiert),
        3. eine neue Anfrage bei lokal aktiver Vorab-Freigabe.

        Eine vom Plugin beendete Anfrage wird nie wiederbelebt — das Schließen im
        Backend wird nachgeholt. Jede andere Anfrage muss der Endkunde bestätigen
        (Repair-Issue wie bei ``connection_request``). Das Session-Ende ist immer
        das früheste aus Server-Fenster, lokaler Obergrenze und ``MAX_SESSION_HOURS``.

        Rückgabe ``False`` heißt: kein Tunnel.
        """
        if self._session_obj is not None and self._session_obj.request_id == request_id:
            return True
        if self._shutting_down:
            return False

        if request_id in self._ended_request_ids:
            if self._log_once(f"ended:{request_id}"):
                _LOGGER.info(
                    "connection_accepted für beendete Anfrage %s — kein Tunnel, "
                    "Schließen im Backend wird nachgeholt",
                    request_id,
                )
            await self._close_request(request_id)
            return False

        now = dt_util.utcnow()
        if session_expires_at is not None and session_expires_at <= now:
            # Das Backend liefert nur gültige Fenster — liegt das Ende aus Sicht der
            # HA-Uhr schon hinter uns, geht die Uhr vor. Fail-closed: nicht verlängern.
            if self._log_once(f"clock:{request_id}"):
                _LOGGER.warning(
                    "connection_accepted für Anfrage %s mit abgelaufenem Session-Ende %s — "
                    "kein Tunnel (Systemuhr von Home Assistant prüfen)",
                    request_id,
                    _iso(session_expires_at),
                )
            return False

        release = self._local_release(request_id, now)
        if release is None:
            await self._ask_customer(request_id, subject, session_expires_at, now)
            return False

        expires_at = self._cap_expiry(release.cap, session_expires_at)
        resume = release.origin == _ORIGIN_RESUME
        if expires_at <= now:
            if resume:
                # Fortsetzbare Session ist inzwischen abgelaufen.
                await self._discard_resumable()
            else:
                await self._mark_ended(request_id)
            return False

        if not await self._activate_user(request_id, resume=resume):
            # Wie in _accept: nicht im 15-s-Takt erneut versuchen, sondern die
            # Anfrage beenden und das Backend informieren.
            await self._mark_ended(request_id)
            return False
        if self._shutting_down or request_id in self._ended_request_ids:
            # Während der Aktivierung beendet (Ablehnung parallel, Unload).
            await self._deactivate_user()
            return False
        self._confirmed.pop(request_id, None)

        _LOGGER.info(
            "Annahme ohne eigene Plugin-Session (request=%s, %s) — Session wird eingerichtet",
            request_id,
            release.origin,
        )
        await self._start_session(
            request_id,
            subject or release.subject,
            release.reason,
            self._hours_until(expires_at, now),
            expires_at=expires_at,
        )
        # Ein evtl. noch offenes Repair-Issue ist hinfällig (#90).
        await self._on_poll_idle()
        return True

    def _local_release(self, request_id: str, now: datetime) -> _LocalRelease | None:
        """Welche lokale Freigabe deckt ``request_id``? ``None`` = keine, Endkunde fragen."""
        resumable = self._resumable
        if resumable is not None and resumable.request_id == request_id:
            return _LocalRelease(
                resumable.ends_at(), resumable.subject, resumable.reason, _ORIGIN_RESUME
            )
        confirmed = self._confirmed.get(request_id)
        if confirmed is not None:
            return _LocalRelease(confirmed, "", "", "Bestätigung des Endkunden")
        if self._pre_auth is not None and self._pre_auth.is_active(now):
            return _LocalRelease(
                now + timedelta(hours=self._pre_auth.max_duration_hours), "", "",
                "Vorab-Freigabe",
            )
        return None

    async def _ask_customer(
        self,
        request_id: str,
        subject: str,
        session_expires_at: datetime | None,
        now: datetime,
    ) -> None:
        """Keine lokale Freigabe: nicht blind dem Backend glauben (widerrufene
        Vorab-Freigabe, die dort nie ankam; kompromittiertes Backend) — der Endkunde
        entscheidet wie bei einer normalen Anfrage (Repair-Issue)."""
        if session_expires_at is not None:
            self._server_expiry[request_id] = session_expires_at
            hours = self._hours_until(session_expires_at, now)
        else:
            # Älteres Backend ohne Session-Ende: die vom Endkunden konfigurierte
            # Maximaldauer vorschlagen, nicht das 720-h-Systemmaximum.
            hours = self._max_duration_hours
        if self._log_once(f"ask:{request_id}"):
            _LOGGER.info(
                "connection_accepted für Anfrage %s ohne lokale Freigabe — "
                "Endkunde muss bestätigen",
                request_id,
            )
        await self._notify_user(request_id, subject, "", hours)

    @staticmethod
    def _cap_expiry(local: datetime, server: datetime | None) -> datetime:
        """Frühestes Ende aus lokaler Obergrenze, Server-Fenster und MAX_SESSION_HOURS."""
        candidates = [local, dt_util.utcnow() + timedelta(hours=MAX_SESSION_HOURS)]
        if server is not None:
            candidates.append(server)
        return min(candidates)

    @staticmethod
    def _hours_until(end: datetime, now: datetime) -> int:
        """Angezeigte Dauer in ganzen Stunden (aufgerundet, 1…MAX_SESSION_HOURS)."""
        hours = math.ceil((end - now).total_seconds() / 3600)
        return max(1, min(MAX_SESSION_HOURS, hours))

    async def _start_session(
        self,
        request_id: str,
        subject: str,
        reason: str,
        duration_hours: int,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        """Eröffnet das Wartungsfenster — Auto-Disable nach `duration_hours`
        bzw. zum Ende ``expires_at``. Die Session wird persistiert, damit sie einen
        HA-Neustart übersteht (#165)."""
        # Abgelöste Anfragen: die bisher laufende und eine nach dem Neustart noch
        # fortsetzbare. Beide sind beendet und dürfen nie wieder aufleben.
        superseded = [
            s.request_id
            for s in (self._session_obj, self._resumable)
            if s is not None and s.request_id != request_id
        ]
        self._resumable = None
        # User NICHT deaktivieren: _accept hat ihn gerade aktiviert; ein evtl.
        # laufender Vorgaenger wird nur abgeloest (Handover/Neustart), nicht beendet.
        await self._end_session(reason="restart", emit=False, deactivate_user=False)

        # Die neue Session steht, BEVOR die Netzwerk-Calls für die abgelösten
        # Anfragen laufen — sonst sähe ein parallel eintreffendes connection_accepted
        # in diesem Await-Fenster keine laufende Session.
        self._session_obj = ActiveSession(
            request_id=request_id,
            subject=subject,
            reason=reason,
            duration_hours=duration_hours,
            expires_at=expires_at,
        )

        delay_s = (
            max(0.0, (expires_at - dt_util.utcnow()).total_seconds())
            if expires_at is not None
            else duration_hours * 3600
        )
        self._session_expire_cancel = async_call_later(
            self._hass,
            delay_s,
            self._on_session_expired,
        )
        await self._persist()
        self._publish_state()
        _LOGGER.info(
            "Wartungsfenster gestartet (request=%s, dauer=%d h)",
            request_id,
            duration_hours,
        )
        for previous in superseded:
            await self._mark_ended(previous)

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
        ended_request_id = None
        if self._session_obj is not None:
            _LOGGER.info(
                "Wartungsfenster beendet (request=%s, grund=%s)",
                self._session_obj.request_id,
                reason,
            )
            ended_request_id = self._session_obj.request_id
            self._session_obj = None
        # Wartungs-User wieder fail-closed deaktivieren (#110, + Refresh-Token-Kill).
        # Beim internen Neustart (reason="restart" aus _start_session) NICHT — dort
        # wird unmittelbar eine neue Session mit frisch aktiviertem User eroeffnet.
        if deactivate_user and self._integrator_user is not None:
            await self._integrator_user.async_deactivate()
        if reason != "restart":
            if ended_request_id:
                # Endkunde trennt, Ablauf, Reconnect-Aufgabe, Ablehnung, Unload: Die
                # Anfrage ist vorbei. Merken + im Backend schließen — der
                # Credentials-DELETE allein ist best-effort und ließ sie sonst
                # ACCEPTED zurück (#165).
                await self._mark_ended(ended_request_id)
            else:
                await self._persist()
            if self._on_session_end is not None:
                # Tunnel schließen (No-op, wenn er schon zu ist — etwa beim
                # Endkunden-Trennen, das dieses Session-Ende ausgelöst hat).
                try:
                    await self._on_session_end()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Tunnel beim Session-Ende nicht geschlossen")
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
        # Ohne Issue wartet die Anfrage nicht mehr auf den Endkunden (Invariante).
        self._server_expiry.pop(request_id, None)
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
                "unlimited": self._unlimited,
            },
        )

    async def async_shutdown(self) -> None:
        if self._reminder_unsub is not None:
            self._reminder_unsub()
            self._reminder_unsub = None
        if self._session_expire_cancel is not None:
            self._session_expire_cancel()
            self._session_expire_cancel = None
        if self._preauth_expire_cancel is not None:
            self._preauth_expire_cancel()
            self._preauth_expire_cancel = None
        self._session_obj = None
        self._pre_auth = None
        self._open_request_id = None
