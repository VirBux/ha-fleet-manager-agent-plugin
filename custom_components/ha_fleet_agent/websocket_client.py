"""WebSocket-Client — ausschließlich für aktive Tunnel-Sessions.

Neue Architektur (Phase 4, TODO #50.21):
- Kein Auto-Connect, kein Heartbeat, kein Reconnect-Backoff, keine Send-Queue.
- Verbindung wird nur beim Tunnel-Aufbau hergestellt:
  connect_for_tunnel(tunnel_token, connector_url) → wartet auf tunnel_open-Frame.
- Disconnect: disconnect() schließt die WS-Verbindung sauber.
- Eingehende Frames werden an registrierte Handler verteilt (tunnel_open, tunnel_data).
- Ausgehende tunnel_data-Frames: send_json().

Der Connector erwartet das Tunnel-Token als Query-Parameter in der URL
(z.B. wss://relay.ha-fleet-manager.com/ws/tunnel?token=<tunnel_token>).
Kein separater Auth-Frame mehr — das Token authentifiziert die Verbindung.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

from .const import MSG_TUNNEL_DATA, MSG_TUNNEL_OPEN

_LOGGER = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class FleetWebSocketClient:
    """Verwaltet die kurzlebige WebSocket-Verbindung für einen einzelnen Tunnel."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        # Wiederverwendete aiohttp-Session (übergeben von __init__.py, nicht selbst verwalten)
        self._session = session

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._read_task: asyncio.Task | None = None

        # Registrierte Handler für eingehende Frames
        self._handlers: dict[str, MessageHandler] = {}

        # Callback, der gefeuert wird wenn der Connector die WS-Verbindung schließt
        self._on_disconnect: Callable[[], None] | None = None

        # SSL-Context — lazy gecached, damit das blockierende create_default_context()
        # nur einmal im Executor-Thread läuft (HA-Best-Practice).
        self._ssl_ctx: ssl.SSLContext | None = None

    # --------------------------------------------------------- Handler-Registrierung

    def register_handler(self, msg_type: str, handler: MessageHandler) -> None:
        """Registriert einen Handler für einen bestimmten Frame-Typ."""
        self._handlers[msg_type] = handler

    def set_disconnect_callback(self, callback: Callable[[], None]) -> None:
        """Setzt den Callback für unerwartete Verbindungstrennungen."""
        self._on_disconnect = callback

    # --------------------------------------------------------- Verbindung

    async def connect_for_tunnel(
        self, tunnel_token: str, connector_url: str
    ) -> None:
        """Baut die WS-Verbindung zum Connector auf und startet die Read-Loop.

        connector_url: vollständige WebSocket-URL des Connectors inkl. Token,
        z.B. "wss://relay.ha-fleet-manager.com/ws/tunnel?token=abc123".
        Alternativ kann tunnel_token separat übergeben werden — dann wird er
        als Query-Parameter an connector_url angehängt (falls noch kein
        `token=`-Parameter enthalten ist).
        """
        if self._ws is not None and not self._ws.closed:
            _LOGGER.warning(
                "connect_for_tunnel aufgerufen, aber WS bereits aktiv — ignoriert"
            )
            return

        # Token als Query-Param anhängen, falls noch nicht in der URL enthalten
        if tunnel_token and "token=" not in connector_url:
            sep = "&" if "?" in connector_url else "?"
            connector_url = f"{connector_url}{sep}token={tunnel_token}"

        # HTTP/2 explizit ausschließen — WebSocket-Upgrade ist HTTP/1.1-only (RFC 6455).
        # ssl.create_default_context() blockiert (load_default_certs + set_default_verify_paths)
        # und darf daher nicht im Event-Loop laufen. Wir erzeugen den Context einmal
        # im Executor und cachen ihn pro Client-Instanz.
        ssl_ctx: ssl.SSLContext | None = None
        if connector_url.startswith("wss://"):
            if self._ssl_ctx is None:
                self._ssl_ctx = await self._hass.async_add_executor_job(
                    self._build_ssl_context
                )
            ssl_ctx = self._ssl_ctx

        _LOGGER.info("Tunnel-WS verbindet: %s", connector_url.split("?")[0])  # Token nicht loggen
        try:
            # heartbeat=30s: sendet automatisch Ping-Frames und erwartet Pongs.
            # Notwendig, damit Traefik die WS-Verbindung nicht nach 180s Idle abreißt
            # (Default-idleTimeout). Ohne Heartbeat stirbt der Tunnel nach ~3 Minuten
            # ohne Traffic, ohne dass das Plugin etwas merkt.
            self._ws = await self._session.ws_connect(
                connector_url,
                ssl=ssl_ctx,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=10.0),
            )
        except Exception as err:
            _LOGGER.warning("WS-Verbindung zum Connector fehlgeschlagen: %s", err)
            raise

        # Read-Loop als Background-Task starten — blockiert nicht den Aufrufer
        self._read_task = self._hass.async_create_background_task(
            self._read_loop(), f"ha_fleet_agent_ws_tunnel_{self._entry_id}"
        )

    async def disconnect(self) -> None:
        """Schließt die WS-Verbindung sauber."""
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — Best-effort
                _LOGGER.debug("WS-Close fehlgeschlagen", exc_info=True)
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._ws = None
        self._read_task = None

    # --------------------------------------------------------- Senden

    async def send_json(self, payload: dict[str, Any]) -> bool:
        """Sendet ein JSON-Payload über die aktive WS-Verbindung.

        Gibt False zurück wenn keine Verbindung aktiv ist (kein Puffern).
        """
        if self._ws is None or self._ws.closed:
            _LOGGER.warning(
                "send_json aufgerufen ohne aktive WS-Verbindung — verworfen"
            )
            return False
        try:
            await self._ws.send_str(json.dumps(payload))
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.warning("WS-Send fehlgeschlagen", exc_info=True)
            return False

    # --------------------------------------------------------- Internas

    async def _read_loop(self) -> None:
        """Liest eingehende Frames bis die Verbindung vom Connector getrennt wird."""
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.info("Tunnel-WS vom Connector geschlossen")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning(
                        "Tunnel-WS Fehler: %s", self._ws.exception()
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unerwarteter Fehler in der WS-Read-Loop")
        finally:
            self._ws = None
            # Disconnect-Callback feuern, damit RemoteAccessManager reagieren kann
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("on_disconnect-Callback fehlgeschlagen", exc_info=True)

    async def _dispatch(self, raw: str) -> None:
        """Verarbeitet einen eingehenden Text-Frame."""
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.debug("Ungültiges JSON ignoriert: %s", raw[:200])
            return

        msg_type: str = data.get("type") or ""
        handler = self._handlers.get(msg_type)
        if handler is not None:
            try:
                await handler(data)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Handler für '%s' ist ausgefallen", msg_type)
        else:
            _LOGGER.debug("Kein Handler registriert für type='%s'", msg_type)

    # --------------------------------------------------------- Properties

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        """Erzeugt den SSL-Context — synchron, läuft im Executor."""
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        return ctx

    @property
    def is_connected(self) -> bool:
        """Gibt True zurück, wenn gerade eine aktive Tunnel-WS-Verbindung besteht."""
        return self._ws is not None and not self._ws.closed

    @property
    def installation_id(self) -> str | None:
        """Kompatibilitäts-Property — im neuen Modell keine installation_id mehr per WS."""
        return None
