"""HA Fleet Agent Integration für Home Assistant.

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

import logging
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
)
from .dashboard import async_ensure_dashboard, async_remove_dashboard
from .device import build_device_info
from .integrator_user import IntegratorUserManager
from .remote_access import RemoteAccessManager
from .request_poller import RequestPoller
from .state_reporter import StateReporter
from .tunnel import TunnelForwarder
from .websocket_client import FleetWebSocketClient

# Storage-Keys für die neuen Module
DATA_INTEGRATOR_USER = "integrator_user"
DATA_TUNNEL_FORWARDER = "tunnel_forwarder"
DATA_STATE_REPORTER = "state_reporter"
DATA_REQUEST_POLLER = "request_poller"
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

GRANT_PREAUTH_SCHEMA = vol.Schema(
    {
        vol.Required("expires_in_hours"): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=168)
        ),
        vol.Optional("max_duration_hours"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=12)
        ),
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
            expires_in_hours=call.data["expires_in_hours"],
            max_duration_hours=call.data.get("max_duration_hours"),
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
    )
    await remote_access.async_load()

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
        "HA Fleet Agent eingerichtet — Backend: %s, Relay: %s",
        backend_url,
        relay_url,
    )
    return True


def _make_connection_accepted_handler(
    hass: HomeAssistant,
    entry_id: str,
    ws_client: FleetWebSocketClient,
    tunnel_forwarder: TunnelForwarder,
    relay_url: str,
    remote_access: RemoteAccessManager,
) -> Any:
    """Erzeugt den Handler für die 'connection_accepted'-Aktion.

    Die Funktion ist eine Closure, damit sie Zugriff auf ws_client und
    tunnel_forwarder hat, ohne diese global zu halten.

    Ablauf bei connection_accepted:
    1. tunnelToken und connectorUrl aus der Poll-Antwort lesen
    2. TunnelForwarder bekommt den Token (für X-Tunnel-Token beim Credentials-POST)
    3. ws_client.connect_for_tunnel(token, url) — baut WS zum Connector auf
       (tunnel_open-Handler im TunnelForwarder wird danach vom WS-Client gefeuert)
    """

    async def _handler(data: dict[str, Any]) -> None:
        # Self-Healing (#90): Hat eine Vorab-Freigabe die Anfrage ohne Endkunden-
        # Klick akzeptiert, kann noch ein Repair-Issue offenstehen — aufraeumen.
        await remote_access._on_poll_idle()

        tunnel_token: str = data.get("tunnelToken") or data.get("tunnel_token") or ""
        # connectorUrl: vollständige WS-URL mit Token, vom Backend geliefert.
        # Falls nicht dabei, aus relay_url + Token ableiten.
        connector_url: str = (
            data.get("connectorUrl")
            or data.get("connector_url")
            or relay_url
        )

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

        # Falls noch eine alte WS aktiv ist (Tunnel wurde nicht ordentlich getrennt):
        # erst sauber schließen, damit der Forwarder DELETE-Credentials für den alten
        # Slug feuert und das Backend den alten ConnectionRequest auf CLOSED setzt.
        # Erst DANACH den neuen Token setzen — der Disconnect-Callback würde ihn
        # sonst sofort wieder auf "" zurücksetzen.
        if ws_client.is_connected:
            _LOGGER.info(
                "Neue Verbindungsanfrage — bestehende Tunnel-WS wird zuerst geschlossen"
            )
            await ws_client.disconnect()

        # Tunnel-Token im Forwarder hinterlegen (für Credentials-POST nach tunnel_open)
        tunnel_forwarder.set_active_tunnel_token(tunnel_token)

        _LOGGER.info(
            "Verbindungsanfrage akzeptiert — baue Tunnel-WS auf (relay=%s)",
            connector_url.split("?")[0],  # Token nicht loggen
        )
        try:
            await ws_client.connect_for_tunnel(tunnel_token, connector_url)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Tunnel-WS-Verbindung fehlgeschlagen")
            tunnel_forwarder.set_active_tunnel_token("")

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
    remote_access: RemoteAccessManager | None = data.get(DATA_REMOTE_ACCESS)
    tunnel_forwarder: TunnelForwarder | None = data.get(DATA_TUNNEL_FORWARDER)
    ws_client: FleetWebSocketClient | None = data.get(DATA_CLIENT)
    integrator_user: IntegratorUserManager | None = data.get(DATA_INTEGRATOR_USER)
    http_session: aiohttp.ClientSession | None = data.get(DATA_HTTP_SESSION)

    if state_reporter is not None:
        state_reporter.stop()
    if request_poller is not None:
        request_poller.stop()
    if remote_access is not None:
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

    _LOGGER.info("HA Fleet Agent entladen (entry_id=%s)", entry.entry_id)
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
