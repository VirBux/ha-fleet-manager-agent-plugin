"""HTTP-Tunnel-Forwarder + Tunnel-Lifecycle-Management.

Verantwortlich für:
- HTTP-Forwarding: tunnel_data/http_request → localhost:8123 → tunnel_data/http_response
- Tunnel-Open-Handler: nach tunnel_open-Frame vom Connector →
  POST /api/agent/tunnels/{slug}/credentials ans Backend (REST statt WS-Frame)
- Tunnel-Close: DELETE /api/agent/tunnels/{slug}/credentials

Designentscheidungen:
- Body wird immer Base64-codiert/decodiert für JSON-Sicherheit bei Binärdaten.
- Host-Header nicht an HA durchreichen.
- KEINE X-Forwarded-*-Header setzen oder durchreichen — Plugin ist kein
  Reverse-Proxy, sondern lokaler Client. HAs http-Component wuerde sonst
  einen Request mit "A request from a reverse proxy was received from ::1,
  but your HTTP integration is not set-up for reverse proxies" mit 400 ablehnen.
- Cookie/Authorization-Header werden durchgereicht — HA macht eigene Session-Auth.
  Cookies sind subdomain-isoliert (tun-*.connector.staging.ha-fleet-manager.com vs.
  staging.ha-fleet-manager.com), daher kein Cookie-Leak des HA-Fleet-Manager-Sessions.
- Credentials werden per REST an POST /api/agent/tunnels/{slug}/credentials
  gesendet (nicht mehr als tunnel_credentials-WS-Frame, Phase 4 #50 §tunnel).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    HA_LOCAL_URL,
    MSG_TUNNEL_CAPABILITIES,
    MSG_TUNNEL_DATA,
    MSG_TUNNEL_OPEN,
    PLUGIN_CAPABILITIES,
    SIGNAL_TUNNEL_STATE,
    TUNNEL_CHUNK_SIZE_BYTES,
    TUNNEL_KIND_HTTP_REQUEST,
    TUNNEL_KIND_HTTP_RESPONSE,
    TUNNEL_KIND_HTTP_RESPONSE_BODY,
    TUNNEL_KIND_WS_ACCEPTED,
    TUNNEL_KIND_WS_CLOSE,
    TUNNEL_KIND_WS_MESSAGE,
    TUNNEL_KIND_WS_OPEN,
    TUNNEL_REQUEST_TIMEOUT_SECONDS,
    WS_CHUNK_SIZE_BYTES,
    WS_OPCODE_BINARY,
    WS_OPCODE_TEXT,
)
from .integrator_user import IntegratorUserManager
from .websocket_client import FleetWebSocketClient

_LOGGER = logging.getLogger(__name__)

# Headers, die wir an HA NICHT weiterreichen.
_DROP_REQUEST_HEADERS = frozenset(
    {
        # Hop-by-hop-Header (RFC 7230 §6.1): gehoeren nicht ueber Proxy-Grenzen.
        "host",
        "connection",
        "keep-alive",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # Proxy-spezifische Auth — nicht fuer Origin-Server.
        "proxy-authenticate",
        "proxy-authorization",
        # X-Forwarded-*: Plugin ist KEIN Reverse-Proxy fuer HA. Der Plugin macht den
        # Forward AS lokaler Client; HA sieht den Request korrekt als from-127.0.0.1.
        # Wuerden wir X-Forwarded-* setzen oder durchreichen, lehnt HAs http-Component
        # den Request mit 400 ab, sofern nicht explizit `trusted_proxies` in der
        # configuration.yaml konfiguriert ist — was wir Endkunden NICHT zumuten wollen.
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "x-forwarded-server",
        "x-forwarded-by",
        "forwarded",  # RFC 7239
        # Origin: Browser sendet beim Asset-Load aus der Tunnel-Subdomain einen fremden
        # Origin (z.B. "https://tun-XXX.connector.staging.ha-fleet-manager.com"). HA lehnt solche
        # Requests mit 403 ab (CORS-Schutz). Das Plugin agiert als lokaler HTTP-Client —
        # kein Origin noetig; ohne Origin-Header antwortet HA korrekt mit 200.
        "origin",
        # Cookie/Authorization werden BEWUSST durchgereicht: HA macht eigene Session-Auth,
        # Browser etabliert die Session beim ersten Login auf der Tunnel-Subdomain. Ohne
        # Cookie-Durchreichung gibt's nach Login 403 auf statische Assets (/frontend_latest/*).
        # Cookies sind subdomain-isoliert (tun-*.connector.* != staging.*) — kein Leak des
        # HA-Fleet-Manager-Session-Cookies.
    }
)


class TunnelForwarder:
    """Registriert Handler für tunnel_open und tunnel_data-Frames."""

    def __init__(
        self,
        hass: HomeAssistant,
        ws_client: FleetWebSocketClient,
        integrator_user: IntegratorUserManager,
        backend_url: str,
        api_key: str,
        http_session: aiohttp.ClientSession,
        *,
        entry_id: str = "",
        local_url: str = HA_LOCAL_URL,
        request_timeout: float = TUNNEL_REQUEST_TIMEOUT_SECONDS,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._ws_client = ws_client
        self._integrator_user = integrator_user
        self._backend_url = backend_url.rstrip("/")
        self._api_key = api_key
        self._local_url = local_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        # Optionaler async-Callback, der beim Tunnel-Close zusätzlich gefeuert wird
        # (z.B. um die laufende Wartungs-Session zu beenden).
        self._on_close = on_close

        # Separate aiohttp-Session für HTTP-Forwards an localhost
        # (Trennung von der globalen Session für Backend-Calls)
        self._http_session: aiohttp.ClientSession = http_session
        self._owns_http_session = False  # Session von außen übergeben — nicht selbst schließen

        # Aktuell aktiver Tunnel-Slug (wird beim tunnel_open gesetzt)
        self._active_tunnel_slug: str | None = None
        # Tunnel-Token des aktuellen Tunnels (für X-Tunnel-Token-Header beim Credentials-POST)
        self._active_tunnel_token: str | None = None

        # In-flight HTTP-Forward-Tasks — werden bei stop() abgebrochen.
        self._pending: set[asyncio.Task] = set()

        # WebSocket-Tunneling (Plugin 0.5.0+): wsId → ClientWebSocketResponse zu HA.
        self._ha_ws: dict[str, aiohttp.ClientWebSocketResponse] = {}
        # wsId → Pump-Task (HA → Connector). Connector → HA laeuft direkt im
        # _on_tunnel_data-Pfad ohne separaten Task.
        self._ws_pump_tasks: dict[str, asyncio.Task] = {}
        # Reassembly-Puffer fuer eingehende ws_message-Chunks (Connector → HA).
        self._ws_incoming_buffers: dict[str, list[bytes]] = {}

        ws_client.register_handler(MSG_TUNNEL_OPEN, self._on_tunnel_open)
        ws_client.register_handler(MSG_TUNNEL_DATA, self._on_tunnel_data)

        # Disconnect-Callback: wenn Connector die WS-Verbindung schließt
        ws_client.set_disconnect_callback(self._on_tunnel_closed)

    def set_active_tunnel_token(self, token: str) -> None:
        """Setzt den Tunnel-Token des aktuellen Tunnels (vom Poller übergeben)."""
        self._active_tunnel_token = token

    async def async_setup(self) -> None:
        """Keine eigene Session mehr nötig — http_session wird von außen übergeben."""
        pass

    async def async_shutdown(self) -> None:
        """Bricht laufende Forwards ab und schliesst alle offenen WS-Verbindungen."""
        # HTTP-Forwards abbrechen
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        self._pending.clear()

        # WS-Pumps stoppen
        for task in list(self._ws_pump_tasks.values()):
            task.cancel()
        if self._ws_pump_tasks:
            await asyncio.gather(
                *self._ws_pump_tasks.values(), return_exceptions=True
            )
        self._ws_pump_tasks.clear()

        # Alle aktiven HA-WS schliessen
        for ha_ws in list(self._ha_ws.values()):
            if not ha_ws.closed:
                try:
                    await ha_ws.close()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("HA-WS-Close fehlgeschlagen", exc_info=True)
        self._ha_ws.clear()
        self._ws_incoming_buffers.clear()

    # ---------------------------------------------------------- Handler

    async def _on_tunnel_open(self, data: dict[str, Any]) -> None:
        """Connector signalisiert einen neuen Tunnel — Credentials per REST ans Backend."""
        # tunnelId enthält den 8-Char-Slug (Connector generiert ihn)
        slug = data.get("tunnelId") or data.get("tunnel_id") or ""
        self._active_tunnel_slug = slug
        self._publish_tunnel_state(True)

        # Capabilities-Frame an Connector. Damit weiss der Connector, dass dieser
        # Plugin-Stand Browser-WS-Upgrades versteht — sonst wuerde er Upgrade-
        # Requests mit 426 Upgrade Required statt 101 beantworten.
        await self._ws_client.send_json({
            "type": MSG_TUNNEL_CAPABILITIES,
            "tunnelId": slug,
            "capabilities": list(PLUGIN_CAPABILITIES),
        })

        await self._integrator_user.async_refresh_status()
        credentials = self._integrator_user.credentials

        if credentials is None:
            _LOGGER.warning(
                "tunnel_open (slug=%s): kein Wartungs-User verfügbar — "
                "Credentials-POST wird nicht gesendet",
                slug,
            )
            return

        if credentials.error:
            _LOGGER.warning(
                "tunnel_open (slug=%s): Wartungs-User-Fehler '%s' — kein POST",
                slug,
                credentials.error,
            )
            return

        await self._post_credentials(slug, credentials.username, credentials.password)

    def _on_tunnel_closed(self) -> None:
        """WS-Verbindung wurde getrennt (vom Connector oder lokal) — DELETE Credentials + Session beenden."""
        slug = self._active_tunnel_slug
        if slug:
            self._hass.async_create_task(self._delete_credentials(slug))
        self._active_tunnel_slug = None
        self._active_tunnel_token = None
        self._publish_tunnel_state(False)
        if self._on_close is not None:
            self._hass.async_create_task(self._safe_on_close())

    async def _safe_on_close(self) -> None:
        try:
            await self._on_close()  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            _LOGGER.exception("on_close-Callback ist ausgefallen")

    async def async_close_tunnel(self) -> bool:
        """Schließt einen aktiven Tunnel auf Initiative des Endkunden.

        Beendet die WS-Verbindung sauber — der bestehende Disconnect-Pfad
        (`_on_tunnel_closed`) übernimmt Credentials-DELETE, Signal-Publish
        und das via on_close injizierte Beenden der Wartungs-Session.

        Gibt True zurück, wenn ein Tunnel aktiv war (also etwas zu tun gab).
        """
        if not self._ws_client.is_connected and self._active_tunnel_slug is None:
            _LOGGER.debug("async_close_tunnel: kein aktiver Tunnel — ignoriert")
            return False
        _LOGGER.info(
            "Tunnel wird vom Endkunden geschlossen (slug=%s)",
            self._active_tunnel_slug or "?",
        )
        await self._ws_client.disconnect()
        return True

    def _publish_tunnel_state(self, open_: bool) -> None:
        """Feuert SIGNAL_TUNNEL_STATE — Entities reagieren reaktiv."""
        async_dispatcher_send(
            self._hass, SIGNAL_TUNNEL_STATE, self._entry_id, open_
        )

    async def _post_credentials(self, slug: str, username: str, password: str) -> None:
        """POST /api/agent/tunnels/{slug}/credentials ans Backend."""
        url = f"{self._backend_url}/api/agent/tunnels/{slug}/credentials"
        headers: dict[str, str] = {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
        }
        # X-Tunnel-Token-Header: Klartext-Token für Backend-Validierung
        if self._active_tunnel_token:
            headers["X-Tunnel-Token"] = self._active_tunnel_token

        body = {"username": username, "password": password}
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with self._http_session.post(
                url, json=body, headers=headers, timeout=timeout
            ) as resp:
                if resp.status in (200, 201, 204):
                    _LOGGER.info(
                        "Tunnel-Credentials für Slug '%s' erfolgreich gepostet", slug
                    )
                else:
                    _LOGGER.warning(
                        "Credentials-POST für Slug '%s' fehlgeschlagen (HTTP %d)",
                        slug,
                        resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Credentials-POST für Slug '%s' Netzwerkfehler: %s", slug, err
            )

    async def _delete_credentials(self, slug: str) -> None:
        """DELETE /api/agent/tunnels/{slug}/credentials — Tunnel-Ende aufräumen."""
        url = f"{self._backend_url}/api/agent/tunnels/{slug}/credentials"
        headers: dict[str, str] = {
            "X-API-Key": self._api_key,
        }
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with self._http_session.delete(
                url, headers=headers, timeout=timeout
            ) as resp:
                if resp.status in (200, 204, 404):
                    # 404 ist ok — Credentials ggf. schon abgelaufen/gelöscht
                    _LOGGER.info(
                        "Tunnel-Credentials für Slug '%s' gelöscht (HTTP %d)",
                        slug,
                        resp.status,
                    )
                else:
                    _LOGGER.warning(
                        "Credentials-DELETE für Slug '%s' fehlgeschlagen (HTTP %d)",
                        slug,
                        resp.status,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Credentials-DELETE für Slug '%s' Netzwerkfehler: %s", slug, err
            )

    async def _on_tunnel_data(self, data: dict[str, Any]) -> None:
        """Dispatcher auf den kind-Diskriminator (HTTP- und WS-Tunneling)."""
        kind = data.get("kind")
        if kind == TUNNEL_KIND_HTTP_REQUEST:
            # Fire-and-forget, damit der WS-Read-Loop nicht blockiert
            task = self._hass.async_create_background_task(
                self._forward(data), name="ha_fleet_agent_tunnel_forward"
            )
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
            return
        if kind == TUNNEL_KIND_WS_OPEN:
            await self._open_ws_to_ha(data)
            return
        if kind == TUNNEL_KIND_WS_MESSAGE:
            await self._relay_ws_message_to_ha(data)
            return
        if kind == TUNNEL_KIND_WS_CLOSE:
            await self._close_ws_from_connector(data)
            return
        _LOGGER.debug("tunnel_data mit unbekanntem kind ignoriert: kind=%s", kind)

    # ---------------------------------------------------------- WS-Tunneling

    async def _open_ws_to_ha(self, data: dict[str, Any]) -> None:
        """Plugin-Pfad fuer ws_open vom Connector: eigene WS-Verbindung zu HA
        aufbauen und Pump-Task fuer HA → Connector starten.

        Bei erfolgreichem aiohttp.ws_connect senden wir ws_accepted an den
        Connector; bei Fehler ws_close mit Code 1011 (Internal Error).
        """
        tunnel_id = data.get("tunnelId", "")
        ws_id = data.get("wsId", "")
        path = data.get("path") or "/"
        headers_in = data.get("headers") or {}

        if not ws_id or not tunnel_id:
            _LOGGER.debug("ws_open ohne tunnelId/wsId verworfen")
            return

        # http://... → ws://..., https://... → wss://...
        base = self._local_url
        if base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        ws_url = f"{base}{path if path.startswith('/') else '/' + path}"

        forward_headers = self._build_ws_forward_headers(headers_in)

        try:
            ha_ws = await self._http_session.ws_connect(
                ws_url, headers=forward_headers
            )
        except Exception as err:  # noqa: BLE001 — aiohttp.WSServerHandshakeError u.a.
            _LOGGER.warning(
                "HA-WS-Upgrade fehlgeschlagen (tunnelId=%s wsId=%s): %s",
                tunnel_id, ws_id, err,
            )
            await self._send_ws_close(tunnel_id, ws_id, 1011, f"ha upgrade failed: {err}")
            return

        self._ha_ws[ws_id] = ha_ws

        # Plugin meldet Connector: HA hat 101 akzeptiert — Pump kann beginnen.
        await self._ws_client.send_json({
            "type": MSG_TUNNEL_DATA,
            "kind": TUNNEL_KIND_WS_ACCEPTED,
            "tunnelId": tunnel_id,
            "wsId": ws_id,
        })

        # Pump-Task: HA → Connector. Connector → HA laeuft direkt im
        # _relay_ws_message_to_ha-Pfad ohne separaten Task.
        task = self._hass.async_create_background_task(
            self._pump_ha_to_connector(tunnel_id, ws_id, ha_ws),
            name=f"ha_fleet_agent_ws_pump_{ws_id}",
        )
        self._ws_pump_tasks[ws_id] = task

    async def _pump_ha_to_connector(
        self,
        tunnel_id: str,
        ws_id: str,
        ha_ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """Liest Frames aus HA und schickt sie als ws_message an den Connector.
        Beim HA-seitigen Close: ws_close an Connector.
        """
        close_code = 1000
        close_reason = "ha closed"
        try:
            async for msg in ha_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._send_ws_message_chunked(
                        tunnel_id, ws_id, WS_OPCODE_TEXT,
                        msg.data.encode("utf-8") if isinstance(msg.data, str)
                        else msg.data,
                    )
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._send_ws_message_chunked(
                        tunnel_id, ws_id, WS_OPCODE_BINARY, msg.data,
                    )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        close_code = 1011
                        close_reason = "ha ws error"
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Fehler in pump_ha_to_connector: wsId=%s", ws_id
            )
            close_code = 1011
            close_reason = "pump exception"
        finally:
            # Pump endet → Connector benachrichtigen + Maps aufraeumen.
            self._ha_ws.pop(ws_id, None)
            self._ws_pump_tasks.pop(ws_id, None)
            self._ws_incoming_buffers.pop(ws_id, None)
            # aiohttp.close_code ist nach Close gesetzt
            if getattr(ha_ws, "close_code", None) is not None:
                close_code = int(ha_ws.close_code)  # type: ignore[arg-type]
            await self._send_ws_close(tunnel_id, ws_id, close_code, close_reason)

    async def _relay_ws_message_to_ha(self, data: dict[str, Any]) -> None:
        """Connector → HA: ws_message-Frame an die offene HA-WS weiterleiten.

        Unterstuetzt Chunking via {@code more}-Feld analog zur HTTP-Pfad-Logik.
        """
        ws_id = data.get("wsId", "")
        ha_ws = self._ha_ws.get(ws_id)
        if ha_ws is None or ha_ws.closed:
            _LOGGER.debug(
                "ws_message fuer unbekannte/geschlossene wsId verworfen: %s", ws_id
            )
            return

        opcode = data.get("opcode") or WS_OPCODE_TEXT
        payload_raw = data.get("payload") or ""
        has_more = bool(data.get("more"))

        if opcode == WS_OPCODE_BINARY:
            try:
                chunk = base64.b64decode(payload_raw) if payload_raw else b""
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Ungueltiges Base64-WS-Binary-Payload verworfen")
                return
        else:
            chunk = (
                payload_raw.encode("utf-8") if isinstance(payload_raw, str)
                else bytes(payload_raw or b"")
            )

        if has_more:
            self._ws_incoming_buffers.setdefault(ws_id, []).append(chunk)
            return

        parts = self._ws_incoming_buffers.pop(ws_id, [])
        parts.append(chunk)
        payload = b"".join(parts)

        try:
            if opcode == WS_OPCODE_BINARY:
                await ha_ws.send_bytes(payload)
            else:
                await ha_ws.send_str(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fehler beim WS-Send an HA (wsId=%s)", ws_id)

    async def _close_ws_from_connector(self, data: dict[str, Any]) -> None:
        """Connector hat ws_close geschickt → HA-Seite schliessen + Pump stoppen."""
        ws_id = data.get("wsId", "")
        code = int(data.get("code") or 1000)

        ha_ws = self._ha_ws.pop(ws_id, None)
        self._ws_incoming_buffers.pop(ws_id, None)
        if ha_ws is not None and not ha_ws.closed:
            try:
                await ha_ws.close(code=code)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("HA-WS-Close fehlgeschlagen (wsId=%s)", ws_id, exc_info=True)
        # Pump-Task wird durch async-for-Break + finally selbst beendet,
        # wir machen aber sicher, dass er auch gecancelt wird falls er haengt.
        task = self._ws_pump_tasks.pop(ws_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _send_ws_close(
        self, tunnel_id: str, ws_id: str, code: int, reason: str
    ) -> None:
        frame: dict[str, Any] = {
            "type": MSG_TUNNEL_DATA,
            "kind": TUNNEL_KIND_WS_CLOSE,
            "tunnelId": tunnel_id,
            "wsId": ws_id,
            "code": int(code),
        }
        if reason:
            frame["reason"] = reason
        await self._ws_client.send_json(frame)

    async def _send_ws_message_chunked(
        self,
        tunnel_id: str,
        ws_id: str,
        opcode: str,
        payload: bytes,
    ) -> None:
        """Sendet ein ws_message-Frame, ggf. gechunkt.

        Text-Frames werden roh als UTF-8-String im payload-Feld uebertragen,
        Binary-Frames Base64-encoded. Bei payload > WS_CHUNK_SIZE_BYTES wird
        in mehrere Frames zerlegt; das letzte Stueck traegt kein 'more'-Feld.
        """
        if not payload:
            await self._ws_client.send_json({
                "type": MSG_TUNNEL_DATA,
                "kind": TUNNEL_KIND_WS_MESSAGE,
                "tunnelId": tunnel_id,
                "wsId": ws_id,
                "opcode": opcode,
                "payload": "",
            })
            return

        chunk_size = WS_CHUNK_SIZE_BYTES
        total = len(payload)
        offset = 0
        while offset < total:
            end = min(offset + chunk_size, total)
            chunk = payload[offset:end]
            has_more = end < total
            if opcode == WS_OPCODE_BINARY:
                payload_field = base64.b64encode(chunk).decode("ascii")
            else:
                payload_field = chunk.decode("utf-8")
            frame: dict[str, Any] = {
                "type": MSG_TUNNEL_DATA,
                "kind": TUNNEL_KIND_WS_MESSAGE,
                "tunnelId": tunnel_id,
                "wsId": ws_id,
                "opcode": opcode,
                "payload": payload_field,
            }
            if has_more:
                frame["more"] = True
            await self._ws_client.send_json(frame)
            offset = end

    @staticmethod
    def _build_ws_forward_headers(headers_in: dict[str, Any]) -> dict[str, str]:
        """Filtert Hop-by-hop und CORS-Trigger fuer den HA-WS-Upgrade.

        Plugin macht den Handshake mit HA selbst — Sec-WebSocket-* gehoeren
        nicht durchgereicht (aiohttp erzeugt die selbst). Cookie/Authorization
        bleiben drin (HA-Session-Auth).
        """
        out: dict[str, str] = {}
        for name, value in headers_in.items():
            if not isinstance(name, str) or not isinstance(value, (str, int, float)):
                continue
            lname = name.lower()
            if lname in _DROP_REQUEST_HEADERS:
                continue
            if lname.startswith("sec-websocket-"):
                continue
            out[name] = str(value)
        return out

    # ---------------------------------------------------------- Forwarding

    async def _forward(self, frame: dict[str, Any]) -> None:
        tunnel_id = frame.get("tunnelId", "")
        req_id = frame.get("reqId", "")
        method = (frame.get("method") or "GET").upper()
        path = frame.get("path") or "/"
        headers_in = frame.get("headers") or {}
        body_b64 = frame.get("body") or ""

        try:
            body = base64.b64decode(body_b64) if body_b64 else b""
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Ungültiger Base64-Body — sende leeren Body an HA")
            body = b""

        url = f"{self._local_url}{path if path.startswith('/') else '/' + path}"
        forward_headers = self._build_forward_headers(headers_in)

        status: int
        resp_headers: dict[str, str]
        resp_body: bytes
        try:
            async with self._http_session.request(
                method,
                url,
                headers=forward_headers,
                data=body if body else None,
                allow_redirects=False,
                timeout=self._timeout,
            ) as response:
                status = response.status
                resp_headers = {k: v for k, v in response.headers.items()}
                resp_body = await response.read()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "HA antwortete nicht innerhalb des Timeouts (tunnelId=%s reqId=%s)",
                tunnel_id,
                req_id,
            )
            status = 504
            resp_headers = {"Content-Type": "text/plain; charset=utf-8"}
            resp_body = b"Gateway Timeout (HA hat nicht geantwortet)"
        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "HA-Forward fehlgeschlagen (tunnelId=%s reqId=%s): %s",
                tunnel_id,
                req_id,
                err,
            )
            status = 502
            resp_headers = {"Content-Type": "text/plain; charset=utf-8"}
            resp_body = f"Bad Gateway: {err}".encode()

        await self._send_chunked_response(
            tunnel_id,
            req_id,
            status,
            self._strip_response_headers(resp_headers),
            resp_body,
        )

    async def _send_chunked_response(
        self,
        tunnel_id: str,
        req_id: str,
        status: int,
        response_headers: dict[str, str],
        body: bytes,
    ) -> None:
        """Splittet den HTTP-Response-Body in `TUNNEL_CHUNK_SIZE_BYTES`-Stücke
        und sendet pro Stück einen WS-Frame.

        Frame-Schema (Plugin 0.4.3 / Connector 2026-05-25):
        - Frame 1: kind=http_response, trägt status/headers + erstes Body-Stück.
        - Frame 2..n: kind=http_response_body, nur tunnelId/reqId/body.
        - Alle Frames bis auf den letzten haben "more": true.
        - Letzter Frame trägt KEIN "more"-Feld → markiert Ende.

        Hintergrund: Quarkus WS-Next Default `max-frame-size=65536`. HA-Asset-
        Responses (>64 KiB) würden den Tunnel mit CorruptedWebSocketFrameException
        sprengen. 32 KiB Chunk-Size lässt nach Base64 (+33 %) genug Puffer
        unterhalb des Limits.
        """
        if not body:
            await self._ws_client.send_json({
                "type": MSG_TUNNEL_DATA,
                "kind": TUNNEL_KIND_HTTP_RESPONSE,
                "tunnelId": tunnel_id,
                "reqId": req_id,
                "status": status,
                "headers": response_headers,
                "body": "",
            })
            return

        chunk_size = TUNNEL_CHUNK_SIZE_BYTES
        total = len(body)
        offset = 0
        is_first = True
        while offset < total:
            end = min(offset + chunk_size, total)
            chunk = body[offset:end]
            has_more = end < total

            if is_first:
                frame: dict[str, Any] = {
                    "type": MSG_TUNNEL_DATA,
                    "kind": TUNNEL_KIND_HTTP_RESPONSE,
                    "tunnelId": tunnel_id,
                    "reqId": req_id,
                    "status": status,
                    "headers": response_headers,
                    "body": base64.b64encode(chunk).decode("ascii"),
                }
                is_first = False
            else:
                frame = {
                    "type": MSG_TUNNEL_DATA,
                    "kind": TUNNEL_KIND_HTTP_RESPONSE_BODY,
                    "tunnelId": tunnel_id,
                    "reqId": req_id,
                    "body": base64.b64encode(chunk).decode("ascii"),
                }
            if has_more:
                frame["more"] = True
            await self._ws_client.send_json(frame)
            offset = end

    @staticmethod
    def _build_forward_headers(headers_in: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in headers_in.items():
            if not isinstance(name, str) or not isinstance(value, (str, int, float)):
                continue
            if name.lower() in _DROP_REQUEST_HEADERS:
                continue
            out[name] = str(value)
        return out

    @staticmethod
    def _strip_response_headers(headers: dict[str, str]) -> dict[str, str]:
        """Entfernt hop-by-hop-Header, damit der Backend-Proxy nichts Doppeltes setzt."""
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in {
                "connection",
                "keep-alive",
                "transfer-encoding",
                "content-length",
                "content-encoding",  # aiohttp dekomprimiert automatisch
            }:
                continue
            out[name] = value
        return out
