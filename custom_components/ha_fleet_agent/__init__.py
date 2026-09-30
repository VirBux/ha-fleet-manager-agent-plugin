"""HA Fleet Manager Agent Integration für Home Assistant.

Phase 4 Architektur (TODO #50):
- Zentrale aiohttp.ClientSession pro Entry (geteilt zwischen StateReporter,
  RequestPoller, RemoteAccessManager, TunnelForwarder)
- StateReporter: REST POST /api/agent/state alle 60 s
- RequestPoller: REST GET /api/agent/poll alle 15 s
- WebSocketClient: nur noch für Tunnel-Sessions (kein Auto-Connect)
- RemoteAccessManager: REST statt WS-Frames
- TunnelForwarder: Credentials per REST statt tunnel_credentials-Frame

Startup-Reihenfolge:
1. aiohttp.ClientSession anlegen
2. FleetWebSocketClient (passiv, kein Auto-Start)
3. IntegratorUserManager.async_setup()
4. TunnelForwarder.async_setup()
5. RemoteAccessManager.async_load()
6. StateReporter.start()
7. RequestPoller.start() + Action-Handler registrieren

Shutdown:
1. StateReporter.stop()
2. RequestPoller.stop()
3. RemoteAccessManager.async_shutdown()
4. TunnelForwarder.async_shutdown()
5. WebSocketClient.disconnect()
6. aiohttp.ClientSession.close()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_at_started

from .const import (
    CONF_API_KEY,
    CONF_BACKEND_URL,
    CONF_RELAY_URL,
    DATA_CLIENT,
    DATA_DEVICE_INFO,
    DATA_REMOTE_ACCESS,
    DOMAIN,
    MAX_PREAUTH_VALIDITY_HOURS,
    MAX_SESSION_HOURS,
    REBUILD_BACKOFF_BASE_SECONDS,
    REBUILD_BACKOFF_MAX_SECONDS,
    REBUILD_SETTLE_SECONDS,
)
from .backup_handler import BackupRequestHandler
from .clear_logs_handler import ClearLogsHandler
from .dashboard import _lang_from_entry, async_ensure_dashboard, async_remove_dashboard
from .device import build_device_info
from .integrator_user import IntegratorUserManager
from .reconnect import TunnelReconnector
from .remote_access import RemoteAccessManager, parse_iso_utc
from .request_poller import RequestPoller
from .restart_handler import RestartHandler
from .state_reporter import StateReporter
from .tunnel import TunnelForwarder
from .update_handler import UpdateCommandHandler
from .websocket_client import FleetWebSocketClient

# Storage-Keys für die neuen Module
DATA_INTEGRATOR_USER = "integrator_user"
DATA_TUNNEL_FORWARDER = "tunnel_forwarder"
DATA_STATE_REPORTER = "state_reporter"
DATA_REQUEST_POLLER = "request_poller"
DATA_UPDATE_HANDLER = "update_handler"
DATA_CLEAR_LOGS_HANDLER = "clear_logs_handler"
DATA_RESTART_HANDLER = "restart_handler"
DATA_BACKUP_HANDLER = "backup_handler"
DATA_RECONNECTOR = "reconnector"
DATA_HTTP_SESSION = "http_session"

# Config-Option: User bei Plugin-Deinstallation behalten?
CONF_KEEP_INTEGRATOR_USER = "keep_integrator_user"

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
]

SERVICE_GRANT_PREAUTH = "grant_pre_authorization"
SERVICE_REVOKE_PREAUTH = "revoke_pre_authorization"
SERVICE_CONFIRM_REQUEST = "confirm_request"
SERVICE_CLOSE_TUNNEL = "close_tunnel"

# Ohne ``expires_in_hours`` gilt die persistierte Gültigkeitsdauer, ohne ``unlimited``
# der Switch „Gültigkeit ohne Ablaufdatum" (#167). Die Sitzungsdauer reicht wie an der Number und
# im Backend bis 720 h (#166; vorher hier 12 h).
GRANT_PREAUTH_SCHEMA = vol.Schema(
    {
        vol.Optional("expires_in_hours"): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=MAX_PREAUTH_VALIDITY_HOURS)
        ),
        vol.Optional("max_duration_hours"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_SESSION_HOURS)
        ),
        vol.Optional("unlimited"): cv.boolean,
    }
)

CONFIRM_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("request_id"): cv.string,
        vol.Required("accepted"): cv.boolean,
        vol.Optional("duration_hours"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=12)
        ),
    }
)


# Das Plugin wird ausschließlich über den Config-Flow eingerichtet (config_flow: true);
# es gibt keine YAML-Konfiguration unter `ha_fleet_agent:`. Das explizite Schema lehnt
# versehentliche YAML-Konfig sauber ab und unterdrückt die hassfest-CONFIG_SCHEMA-Warnung.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """YAML-Setup (nicht genutzt) — wir registrieren hier nur die Services."""
    hass.data.setdefault(DOMAIN, {})

    async def _resolve_manager(call: ServiceCall) -> RemoteAccessManager | None:
        # Service ist global registriert — fällt aktuell auf den ersten Entry zurück.
        # Bei zukünftiger Multi-Entry-Unterstützung muss der Aufrufer entry_id mitgeben.
        entries: list[dict] = list(hass.data.get(DOMAIN, {}).values())
        entries = [e for e in entries if isinstance(e, dict) and DATA_REMOTE_ACCESS in e]
        if not entries:
            _LOGGER.warning("Kein aktiver Fleet-Agent-Entry — Service ignoriert")
            return None
        return entries[0][DATA_REMOTE_ACCESS]

    async def _grant_preauth(call: ServiceCall) -> None:
        manager = await _resolve_manager(call)
        if manager is None:
            return
        await manager.grant_pre_authorization(
            expires_in_hours=call.data.get("expires_in_hours"),
            max_duration_hours=call.data.get("max_duration_hours"),
            unlimited=call.data.get("unlimited"),
        )

    async def _revoke_preauth(call: ServiceCall) -> None:
        manager = await _resolve_manager(call)
        if manager is None:
            return
        await manager.revoke_pre_authorization()

    async def _confirm_request(call: ServiceCall) -> None:
        manager = await _resolve_manager(call)
        if manager is None:
            return
        await manager.confirm_request(
            request_id=call.data["request_id"],
            accepted=call.data["accepted"],
            duration_hours=call.data.get("duration_hours"),
        )

    async def _close_tunnel(call: ServiceCall) -> None:
        entries: list[dict] = list(hass.data.get(DOMAIN, {}).values())
        entries = [
            e for e in entries if isinstance(e, dict) and DATA_TUNNEL_FORWARDER in e
        ]
        if not entries:
            _LOGGER.warning("Kein aktiver Fleet-Agent-Entry — close_tunnel ignoriert")
            return
        forwarder: TunnelForwarder = entries[0][DATA_TUNNEL_FORWARDER]
        await forwarder.async_close_tunnel()

    hass.services.async_register(
        DOMAIN, SERVICE_GRANT_PREAUTH, _grant_preauth, schema=GRANT_PREAUTH_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_REVOKE_PREAUTH, _revoke_preauth)
    hass.services.async_register(
        DOMAIN, SERVICE_CONFIRM_REQUEST, _confirm_request, schema=CONFIRM_REQUEST_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CLOSE_TUNNEL, _close_tunnel)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Entry laden: REST-Reporter + Poller + Tunnel-Client starten."""
    hass.data.setdefault(DOMAIN, {})

    api_key: str = entry.data[CONF_API_KEY]
    backend_url: str = entry.data[CONF_BACKEND_URL]
    relay_url: str = entry.data.get(CONF_RELAY_URL, "")

    # Zentrale aiohttp-Session — wird von allen Modulen wiederverwendet.
    # Wichtig: HTTP/1.1 für Backend-REST (kein WebSocket hier), HTTP/2 ok.
    http_session = aiohttp.ClientSession()

    # WebSocket-Client — passiv, kein Auto-Start. Nur für Tunnel-Sessions.
    ws_client = FleetWebSocketClient(hass, entry.entry_id, http_session)

    # Wartungs-User + Tunnel-Forwarder
    integrator_user = IntegratorUserManager(hass, entry.entry_id)
    try:
        await integrator_user.async_setup()
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Wartungs-User konnte nicht angelegt werden — Tunnel funktioniert ohne Credentials"
        )

    # RemoteAccessManager zuerst — TunnelForwarder hängt einen Close-Callback rein,
    # damit ein manueller Tunnel-Abbruch zugleich die Wartungs-Session beendet
    # (REQUIREMENTS §4.4 — Endkunden-Abbruch).
    remote_access = RemoteAccessManager(
        hass,
        entry.entry_id,
        session=http_session,
        backend_url=backend_url,
        api_key=api_key,
        integrator_user=integrator_user,
        language=_lang_from_entry(entry, hass),
    )
    await remote_access.async_load()
    # Erinnerung an eine dauerhafte Vorab-Freigabe (#167): täglicher Check.
    remote_access.async_start_reminder()

    async def _on_tunnel_closed() -> None:
        await remote_access.async_end_session(reason="tunnel_closed")

    tunnel_forwarder = TunnelForwarder(
        hass,
        ws_client,
        integrator_user,
        backend_url=backend_url,
        api_key=api_key,
        http_session=http_session,
        entry_id=entry.entry_id,
        on_close=_on_tunnel_closed,
    )
    await tunnel_forwarder.async_setup()

    # StateReporter — REST POST alle 60 s
    state_reporter = StateReporter(
        hass,
        entry.entry_id,
        session=http_session,
        backend_url=backend_url,
        api_key=api_key,
    )

    # RequestPoller — REST GET alle 15 s
    request_poller = RequestPoller(
        hass,
        session=http_session,
        backend_url=backend_url,
        api_key=api_key,
    )

    # Reconnector (#108 Phase C): stößt nach unerwartetem Tunnel-Abriss einen
    # sofortigen Re-Poll an (statt bis zu 15 s zu warten) — der reguläre
    # connection_accepted-Handler baut den Tunnel dann auf. Greift nur, solange
    # die Wartungs-Session noch läuft; der 15-s-Poll bleibt Fallback.
    async def _reconnect_gave_up() -> None:
        await remote_access.async_end_session(reason="reconnect_failed")

    reconnector = TunnelReconnector(
        hass,
        poll_once=request_poller._poll_once,
        is_tunnel_up=lambda: ws_client.is_connected,
        is_session_open=lambda: remote_access.session is not None,
        on_give_up=_reconnect_gave_up,
    )
    tunnel_forwarder.set_reconnect_callback(reconnector.trigger)
    # Session-Ende im Plugin (Ablauf, Ablehnung, Unload) schließt den Tunnel (#165).
    remote_access.set_session_end_callback(tunnel_forwarder.async_close_tunnel)

    # Action-Handler beim Poller registrieren
    request_poller.register_handler(
        "connection_request", remote_access._on_connection_request
    )
    request_poller.register_handler(
        "connection_accepted",
        _make_connection_accepted_handler(
            hass, entry.entry_id, ws_client, tunnel_forwarder, relay_url, remote_access
        ),
    )
    # Self-Healing-Handler (#90): raeumt verwaiste Repair-Issues, sobald der Poll
    # "nichts offen" (HTTP 204 → synthetische "idle"-Aktion) meldet.
    request_poller.register_handler("idle", remote_access._on_poll_idle)

    # Abgleich der Vorab-Freigabe (#167): Jede Poll-Antwort trägt die Backend-Sicht.
    request_poller.add_response_listener(remote_access.async_on_poll_response)
    # Update-Befehle (#103): das Backend liefert in ruhigen Ticks (keine
    # Connection-Action offen) die Action "update_batch" mit allen offenen
    # Update-Commands; der Handler arbeitet sie sequenziell via update.install ab.
    update_handler = UpdateCommandHandler(
        hass,
        session=http_session,
        backend_url=backend_url,
        api_key=api_key,
    )
    request_poller.register_handler("update_batch", update_handler.handle)
    # Log-Leeren (#109): das Backend liefert in ruhigen Ticks die Action
    # "clear_logs", wenn der Integrator im Dashboard "Logs leeren" geklickt hat.
    # Der Handler ruft system_log.clear und stoesst danach einen sofortigen
    # State-Push an (frischer, leerer Log-Snapshot ohne Wartezeit).
    clear_logs_handler = ClearLogsHandler(hass, state_reporter)
    request_poller.register_handler("clear_logs", clear_logs_handler.handle)
    # System-Neustart (#127, Sofort-Weg): das Backend liefert in ruhigen Ticks die
    # Action "restart", wenn der Integrator im Dashboard "System neu starten" bestaetigt
    # hat. Der Handler ruft homeassistant.restart — danach ist der Agent bis zum
    # Wiederanlauf offline (kein State-Push mehr moeglich).
    # Der Handler quittiert den Neustart vor dem Service-Aufruf ans Backend (#127,
    # Rueckmeldung) — dafuer braucht er Session, Backend-URL und Key.
    restart_handler = RestartHandler(
        hass,
        session=http_session,
        backend_url=backend_url,
        api_key=api_key,
    )
    request_poller.register_handler("restart", restart_handler.handle)
    # Backup auf Knopfdruck (#168): das Backend liefert direkt nach den Update-Befehlen
    # die Action "backup_create". Der Handler erzeugt das Backup verschlüsselt über den
    # Backup-Manager von HA, lädt es in Stücken hoch und löscht es danach lokal. Sein
    # Zustand überlebt HA-Neustarts; nach dem Start setzt er einen Upload fort.
    backup_handler = BackupRequestHandler(
        hass,
        entry.entry_id,
        session=http_session,
        backend_url=backend_url,
        api_key=api_key,
        language=_lang_from_entry(entry, hass),
    )
    await backup_handler.async_setup()
    request_poller.register_handler("backup_create", backup_handler.handle)

    hass.data[DOMAIN][entry.entry_id] = {
        CONF_API_KEY: api_key,
        CONF_BACKEND_URL: backend_url,
        DATA_HTTP_SESSION: http_session,
        DATA_CLIENT: ws_client,
        DATA_REMOTE_ACCESS: remote_access,
        DATA_INTEGRATOR_USER: integrator_user,
        DATA_TUNNEL_FORWARDER: tunnel_forwarder,
        DATA_STATE_REPORTER: state_reporter,
        DATA_REQUEST_POLLER: request_poller,
        DATA_RECONNECTOR: reconnector,
        DATA_UPDATE_HANDLER: update_handler,
        DATA_CLEAR_LOGS_HANDLER: clear_logs_handler,
        DATA_RESTART_HANDLER: restart_handler,
        DATA_BACKUP_HANDLER: backup_handler,
        DATA_DEVICE_INFO: build_device_info(entry.entry_id, backend_url),
    }

    state_reporter.start()
    request_poller.start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Auto-Dashboard "Fernwartung" (REQUIREMENTS §4.6, TODO #91).
    # Direkt versuchen — bei laufendem HA sind sowohl Entities als auch
    # hass.data["lovelace"] verfuegbar. Beim ersten Start nach HA-Boot fehlt
    # die Lovelace-Struktur teilweise noch; in dem Fall greift der
    # async_at_started-Fallback und versucht es nach dem Start erneut.
    try:
        await async_ensure_dashboard(hass, entry)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Auto-Dashboard konnte nicht angelegt werden — Setup laeuft trotzdem weiter"
        )

    async def _ensure_dashboard_after_start(_hass: HomeAssistant) -> None:
        try:
            await async_ensure_dashboard(_hass, entry)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Dashboard-Setup nach HA-Start fehlgeschlagen")

    async_at_started(hass, _ensure_dashboard_after_start)

    _LOGGER.info(
        "HA Fleet Manager Agent eingerichtet — Backend: %s, Relay: %s",
        backend_url,
        relay_url,
    )
    return True


@dataclass
class _RebuildBackoff:
    """Zählt Neuaufbauten derselben Anfrage und bremst sie exponentiell (#165).

    Der erste Neuaufbau läuft sofort, danach mindestens 15 s, 30 s, 60 s … bis
    5 min Pause. Eine andere requestId setzt den Zähler zurück.
    """

    request_id: str | None = None
    count: int = 0
    last: float = 0.0

    def wait_remaining(self, request_id: str, now: float) -> float:
        """Verbleibende Pause in Sekunden (0 = Neuaufbau erlaubt)."""
        if request_id != self.request_id or self.count == 0:
            return 0.0
        if now - self.last > 2 * REBUILD_BACKOFF_MAX_SECONDS:
            # Lange kein Neuaufbau nötig: Der Tunnel lief — späterer Bedarf (etwa
            # nach einem Backend-Neustart) beginnt wieder bei der ersten Stufe.
            self.count = 0
            return 0.0
        wait_s = min(
            REBUILD_BACKOFF_MAX_SECONDS,
            REBUILD_BACKOFF_BASE_SECONDS * 2 ** (self.count - 1),
        )
        return max(0.0, wait_s - (now - self.last))

    def record_attempt(self, request_id: str, now: float) -> int:
        """Vermerkt einen Neuaufbau und liefert seine laufende Nummer."""
        if request_id != self.request_id:
            self.request_id, self.count = request_id, 0
        self.count += 1
        self.last = now
        return self.count

    def mark_done(self, now: float) -> None:
        """Pause ab dem Abbau des alten Tunnels messen, nicht ab dem Poll-Eingang —
        sonst würde die erste 15-s-Stufe wegen der Poll-Latenz oft zu 30 s."""
        self.last = now


def _make_connection_accepted_handler(
    hass: HomeAssistant,
    entry_id: str,
    ws_client: FleetWebSocketClient,
    tunnel_forwarder: TunnelForwarder,
    relay_url: str,
    remote_access: RemoteAccessManager,
    *,
    rebuild_settle_s: float = REBUILD_SETTLE_SECONDS,
    monotonic: Any = time.monotonic,
) -> Any:
    """Erzeugt den Handler für die 'connection_accepted'-Aktion.

    Die Funktion ist eine Closure, damit sie Zugriff auf ws_client und
    tunnel_forwarder hat, ohne diese global zu halten.

    Ablauf bei connection_accepted:
    1. tunnelToken und connectorUrl aus der Poll-Antwort lesen
    2. Freigabe prüfen und Wartungs-Session sicherstellen — auch wenn das Plugin
       die Annahme nicht selbst ausgelöst hat (Vorab-Freigabe, HA-Neustart).
       Ohne lokale Freigabe muss der Endkunde bestätigen (#165).
    3. TunnelForwarder bekommt den Token (für X-Tunnel-Token beim Credentials-POST)
    4. ws_client.connect_for_tunnel(token, url) — baut WS zum Connector auf
       (tunnel_open-Handler im TunnelForwarder wird danach vom WS-Client gefeuert)
    """

    # Neuaufbau-Bremse (#165): Kommen die Zugangsdaten dauerhaft nicht an (POST
    # scheitert, Wartungs-User gesperrt), soll nicht alle 15 s ein Tunnel fallen
    # und neu entstehen.
    rebuild = _RebuildBackoff()

    async def _handler(data: dict[str, Any]) -> None:
        # requestId der Anfrage (camelCase vom Backend; snake_case-Fallback für Tests).
        request_id: str = data.get("requestId") or data.get("request_id") or ""
        tunnel_token: str = data.get("tunnelToken") or data.get("tunnel_token") or ""
        # connectorUrl: vollständige WS-URL mit Token, vom Backend geliefert.
        # Falls nicht dabei, aus relay_url + Token ableiten.
        connector_url: str = (
            data.get("connectorUrl")
            or data.get("connector_url")
            or relay_url
        )
        # Phase D: vom Backend vorgegebener stabiler Slug (optional). Sorgt nach
        # einem Reconnect für die GLEICHE Tunnel-URL. Älteres Backend liefert ihn
        # nicht — dann würfelt der Connector wie bisher.
        slug: str = data.get("slug") or ""

        if not tunnel_token:
            _LOGGER.warning(
                "connection_accepted empfangen ohne tunnelToken — ignoriert"
            )
            return

        if not connector_url:
            _LOGGER.warning(
                "connection_accepted: keine connectorUrl und keine relay_url — ignoriert"
            )
            return

        # Wartungs-Session sicherstellen, BEVOR ein Tunnel steht: Bei Vorab-Freigabe
        # und nach einem HA-Neustart kennt das Plugin die Annahme nur aus diesem
        # Frame. Fail-closed — ist der Wartungs-User nicht aktivierbar, kein Tunnel.
        if not await remote_access.async_ensure_session_for_accepted(
            request_id,
            subject=data.get("subject") or "",
            session_expires_at=parse_iso_utc(data.get("sessionExpiresAt")),
        ):
            # Keine Freigabe (beendete Anfrage, Rückfrage offen, Uhr geht vor). Steht
            # für genau diese Anfrage noch eine Tunnel-WS, schließen (#165).
            if ws_client.is_connected and request_id == tunnel_forwarder.active_request_id:
                await tunnel_forwarder.async_close_tunnel()
            return

        def _session_open() -> bool:
            # Endet die Session während der folgenden Awaits (Ablauf, Ablehnung,
            # Trennen, Unload), ist async_close_tunnel ein No-op, solange die WS noch
            # nicht steht — der Handler darf dann nicht (weiter) verbinden (#165).
            session = remote_access.session
            return session is not None and session.request_id == request_id

        if ws_client.is_connected:
            if request_id and request_id == tunnel_forwarder.active_request_id:
                # Erneutes connection_accepted für die LAUFENDE Anfrage: Das Backend
                # gibt nur dann einen Token aus, wenn es keinen Live-Tunnel für sie
                # kennt — die Zugangsdaten sind nie angekommen (z.B. Wartungs-User
                # war deaktiviert) oder der Backend-Cache ist nach einem Neustart
                # leer. Früher wurde der Frame hier ignoriert; dann blieb dieser
                # Zustand dauerhaft hängen („Session aktiv, keine Live-Verbindung",
                # alle 15 s ein neuer Token). Stattdessen neu aufbauen: Der stabile
                # Slug hält die Tunnel-URL gleich, tunnel_open postet die
                # Zugangsdaten mit dem frischen Token.
                wait_s = rebuild.wait_remaining(request_id, monotonic())
                if wait_s > 0:
                    _LOGGER.debug(
                        "Neuaufbau für request=%s gebremst (noch %.0f s Pause)",
                        request_id,
                        wait_s,
                    )
                    return
                attempt = rebuild.record_attempt(request_id, monotonic())
                _LOGGER.info(
                    "Backend kennt den laufenden Tunnel nicht (request=%s) — "
                    "Tunnel wird neu aufgebaut (%d. Versuch)",
                    request_id,
                    attempt,
                )
                tunnel_forwarder.mark_rebuild_close()
                await ws_client.disconnect()
                rebuild.mark_done(monotonic())
                # Gleicher Slug: Der Close-Notify des alten Tunnels muss beim Backend
                # sein, bevor der neue seine Zugangsdaten postet — er matcht nur über
                # den Slug und würde sonst den Cache-Eintrag des neuen Tunnels
                # abräumen. Die Pause gibt auch dem Connector Zeit, den Slug
                # freizugeben (sonst würfelt er einen neuen, die URL wechselt).
                if rebuild_settle_s > 0:
                    await asyncio.sleep(rebuild_settle_s)
            else:
                # Andere requestId → echte neue Anfrage: alte WS erst sauber schließen,
                # damit der Forwarder DELETE-Credentials für den alten Slug feuert und
                # das Backend den alten ConnectionRequest auf CLOSED setzt. Erst DANACH
                # den neuen Zustand setzen — der Disconnect-Callback würde ihn sonst
                # sofort wieder zurücksetzen.
                # Handover markieren (#108 Phase C): der alte Tunnel-Close darf WEDER
                # die (bereits neue) Wartungs-Session beenden NOCH einen Reconnect
                # auslösen — diesen Aufbau übernehmen wir gleich selbst.
                _LOGGER.info(
                    "Neue Verbindungsanfrage — bestehende Tunnel-WS wird zuerst geschlossen"
                )
                tunnel_forwarder.mark_handover_close()
                await ws_client.disconnect()

        # Zustand des neuen Tunnels im Forwarder hinterlegen: Token für den
        # Credentials-POST, requestId für Handover/Neuaufbau. Den Slug gibt der Connect
        # direkt an den Connector weiter (autoritativ wird er dann via tunnel_open).
        if not _session_open():
            _LOGGER.info(
                "Wartungs-Session von request=%s endete während des Tunnel-Aufbaus — "
                "kein Tunnel",
                request_id,
            )
            return

        tunnel_forwarder.set_active_tunnel_token(tunnel_token)
        tunnel_forwarder.set_active_request_id(request_id)

        _LOGGER.info(
            "Verbindungsanfrage akzeptiert — baue Tunnel-WS auf (relay=%s)",
            connector_url.split("?")[0],  # Token nicht loggen
        )
        try:
            await ws_client.connect_for_tunnel(tunnel_token, connector_url, slug=slug)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Tunnel-WS-Verbindung fehlgeschlagen")
            tunnel_forwarder.set_active_tunnel_token("")
            tunnel_forwarder.set_active_request_id(None)
            return
        if not _session_open():
            # Session endete während des Handshakes → den eben aufgebauten Tunnel
            # sofort wieder schließen, sonst bliebe er ohne Session offen.
            await tunnel_forwarder.async_close_tunnel()

    return _handler


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Entry entladen: alle Komponenten stoppen, Session schließen."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if data is None:
        return True

    state_reporter: StateReporter | None = data.get(DATA_STATE_REPORTER)
    request_poller: RequestPoller | None = data.get(DATA_REQUEST_POLLER)
    reconnector: TunnelReconnector | None = data.get(DATA_RECONNECTOR)
    remote_access: RemoteAccessManager | None = data.get(DATA_REMOTE_ACCESS)
    tunnel_forwarder: TunnelForwarder | None = data.get(DATA_TUNNEL_FORWARDER)
    ws_client: FleetWebSocketClient | None = data.get(DATA_CLIENT)
    integrator_user: IntegratorUserManager | None = data.get(DATA_INTEGRATOR_USER)
    http_session: aiohttp.ClientSession | None = data.get(DATA_HTTP_SESSION)

    if state_reporter is not None:
        state_reporter.stop()
    if request_poller is not None:
        request_poller.stop()
    # Laufenden Backup-Upload anhalten, bevor die HTTP-Session schließt (#168). Der
    # Auftragszustand bleibt gespeichert; nach dem nächsten Laden setzt er fort.
    backup_handler: BackupRequestHandler | None = data.get(DATA_BACKUP_HANDLER)
    if backup_handler is not None:
        await backup_handler.async_shutdown()
    # Reconnect-Loop stoppen, bevor der Tunnel-Forwarder schließt (sonst könnte ein
    # laufender Loop noch pollen, während alles abgebaut wird) — #108 Phase C.
    if reconnector is not None:
        reconnector.cancel()
    if remote_access is not None:
        # Unload (Reload, Deaktivieren, Entfernen — nicht der normale HA-Stopp) ist
        # ein gewolltes Ende: Session beenden statt sie als fortsetzbar liegen zu
        # lassen. Läuft vor dem Schließen der HTTP-Session, damit der Close ans
        # Backend durchgeht (#165).
        await remote_access.async_end_for_unload()
        await remote_access.async_shutdown()
    if tunnel_forwarder is not None:
        await tunnel_forwarder.async_shutdown()
    if ws_client is not None:
        await ws_client.disconnect()
    if integrator_user is not None:
        keep_user = bool(entry.options.get(CONF_KEEP_INTEGRATOR_USER, False))
        await integrator_user.async_remove(keep_user=keep_user)
    if http_session is not None and not http_session.closed:
        await http_session.close()

    _LOGGER.info("HA Fleet Manager Agent entladen (entry_id=%s)", entry.entry_id)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wird beim vollstaendigen Entfernen der Integration aufgerufen.

    Loescht das Auto-Dashboard (REQUIREMENTS §4.6 / TODO #91). Bewusst NICHT
    in async_unload_entry — sonst verschwindet das Dashboard auch bei jedem
    Reload und Kunden-Anpassungen waeren weg.
    """
    try:
        await async_remove_dashboard(hass, entry)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Konnte Fernwartungs-Dashboard beim Entfernen nicht aufraeumen"
        )
