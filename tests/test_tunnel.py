"""Unit-Tests fuer den TunnelForwarder (Phase 4 — REST-Credentials)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from ha_fleet_agent import const
from ha_fleet_agent.tunnel import TunnelForwarder


# --------------------------------------------------------- Stubs


class FakeIntegratorUser:
    """Minimaler IntegratorUserManager-Stub fuer Tests."""

    def __init__(self, credentials=None):
        self._credentials = credentials

    @property
    def credentials(self):
        return self._credentials

    async def async_refresh_status(self) -> None:
        return None


class FakeCredentials:
    def __init__(self, username="ha-fleet-integrator", password="secret",
                 active=True, error=None):
        self.username = username
        self.password = password
        self.active = active
        self.error = error

    def to_frame_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"username": self.username}
        if self.error is not None:
            out["error"] = self.error
        else:
            out["password"] = self.password
        return out


class FakeWsClient:
    """WebSocket-Client-Stub fuer den TunnelForwarder."""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self._handlers: dict[str, Any] = {}
        self._disconnect_callback = None
        self.is_connected = False
        self.disconnect_calls = 0

    def register_handler(self, msg_type, handler):
        self._handlers[msg_type] = handler

    def set_disconnect_callback(self, callback):
        self._disconnect_callback = callback

    async def send_json(self, payload):
        self.sent.append(payload)
        return True

    async def disconnect(self):
        """Simuliert das saubere Schliessen — feuert den Disconnect-Callback."""
        self.disconnect_calls += 1
        self.is_connected = False
        if self._disconnect_callback is not None:
            self._disconnect_callback()


class FakeHttpSession:
    """aiohttp.ClientSession-Stub mit getrennten Handlern fuer HTTP-Forward und REST."""

    def __init__(self, http_handler=None, rest_handler=None, ws_handler=None):
        """http_handler: Antwort fuer localhost-Requests.
        rest_handler: aufgerufen fuer Backend-REST-Calls (POST/DELETE Credentials etc.).
        ws_handler: aufgerufen fuer aiohttp.ClientSession.ws_connect — kann eine
        FakeHaWs liefern oder eine Exception werfen (Upgrade-Fehler simulieren).
        """
        self._http_handler = http_handler or _default_handler
        self._rest_handler = rest_handler
        self._ws_handler = ws_handler
        self.closed = False
        self.calls: list[dict] = []
        # Getrenntes Tracking fuer REST-Calls
        self.rest_calls: list[dict] = []
        # Tracking fuer ws_connect-Calls
        self.ws_calls: list[dict] = []

    def request(self, method, url, headers=None, data=None, allow_redirects=False,
                timeout=None):
        call = {
            "method": method,
            "url": url,
            "headers": headers or {},
            "data": data,
            "allow_redirects": allow_redirects,
        }
        self.calls.append(call)
        return self._http_handler(call)

    def post(self, url, json=None, headers=None, timeout=None):
        call = {"method": "POST", "url": url, "json": json, "headers": headers or {}}
        self.rest_calls.append(call)
        if self._rest_handler:
            return self._rest_handler(call)
        return _FakeResponse(204, b"", {})

    def delete(self, url, headers=None, timeout=None):
        call = {"method": "DELETE", "url": url, "headers": headers or {}}
        self.rest_calls.append(call)
        if self._rest_handler:
            return self._rest_handler(call)
        return _FakeResponse(204, b"", {})

    async def ws_connect(self, url, headers=None, **kwargs):
        call = {"url": url, "headers": headers or {}}
        self.ws_calls.append(call)
        if self._ws_handler is None:
            return FakeHaWs()
        return await self._ws_handler(call)

    async def close(self):
        self.closed = True


class FakeHaWs:
    """aiohttp.ClientWebSocketResponse-Stub.

    Simuliert die WS-Verbindung zur lokalen HA. Der Test pumpt eingehende
    Frames per push_text/push_binary/push_close in die interne Queue; die
    Pump-Logik im TunnelForwarder konsumiert sie via async-for.
    """

    def __init__(self):
        import aiohttp as _aiohttp
        self._aiohttp = _aiohttp
        self._queue: asyncio.Queue = asyncio.Queue()
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self.close_code: int | None = None
        self._iter_closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self._queue.get()
        if msg.type in (
            self._aiohttp.WSMsgType.CLOSED,
            self._aiohttp.WSMsgType.CLOSING,
            self._aiohttp.WSMsgType.CLOSE,
        ):
            self._iter_closed = True
            self.closed = True
            return msg
        if msg.type == self._aiohttp.WSMsgType.ERROR:
            self._iter_closed = True
            return msg
        return msg

    async def send_str(self, data: str):
        self.sent_text.append(data)

    async def send_bytes(self, data: bytes):
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000, message: bytes = b""):
        self.closed = True
        self.close_code = code

    def push_text(self, text: str):
        msg = self._aiohttp.WSMessage(
            self._aiohttp.WSMsgType.TEXT, text, ""
        )
        self._queue.put_nowait(msg)

    def push_binary(self, data: bytes):
        msg = self._aiohttp.WSMessage(
            self._aiohttp.WSMsgType.BINARY, data, ""
        )
        self._queue.put_nowait(msg)

    def push_close(self, code: int = 1000):
        # aiohttp signalisiert HA-seitigen Close ueber WSMsgType.CLOSED
        msg = self._aiohttp.WSMessage(
            self._aiohttp.WSMsgType.CLOSED, code, ""
        )
        self.close_code = code
        self._queue.put_nowait(msg)


class FakeHass:
    """Minimaler hass-Stub."""

    def __init__(self):
        self._tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name=None):
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task

    def async_create_task(self, coro):
        task = asyncio.get_event_loop().create_task(coro)
        self._tasks.append(task)
        return task


# --------------------------------------------------------- Fake aiohttp-Response


class _FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]):
        self.status = status
        self._body = body
        self.headers = headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def read(self) -> bytes:
        return self._body


def _default_handler(call):
    return _FakeResponse(200, b"OK", {"Content-Type": "text/plain"})


# --------------------------------------------------------- Fixtures


@pytest.fixture
def make_forwarder():
    """Factory fuer den TunnelForwarder mit injizierten Stubs."""

    def _make(http_handler=None, rest_handler=None, ws_handler=None,
              credentials=None, request_timeout=5, tunnel_token="test-token"):
        ws_client = FakeWsClient()
        user = FakeIntegratorUser(credentials)
        hass = FakeHass()
        http_session = FakeHttpSession(http_handler, rest_handler, ws_handler)

        fwd = TunnelForwarder(
            hass,
            ws_client,
            user,
            backend_url="https://api.ha-fleet-manager.com",
            api_key="test-api-key-1234567890",
            http_session=http_session,
            local_url="http://ha.test",
            request_timeout=request_timeout,
        )
        fwd.set_active_tunnel_token(tunnel_token)
        return fwd, ws_client, http_session, hass

    return _make


# --------------------------------------------------------- Tests: Credentials per REST


@pytest.mark.asyncio
async def test_tunnel_open_postet_credentials_per_rest(make_forwarder):
    """Nach tunnel_open sollen Credentials per POST ans Backend gehen (nicht per WS-Frame).
    Plugin 0.5.0: zusaetzlich wird ein tunnel_capabilities-WS-Frame gesendet."""
    rest_calls: list[dict] = []

    def rest_handler(call):
        rest_calls.append(call)
        return _FakeResponse(204, b"", {})

    fwd, ws_client, http_session, _ = make_forwarder(
        credentials=FakeCredentials(password="topsecret"),
        rest_handler=rest_handler,
    )

    await fwd._on_tunnel_open({"tunnelId": "abc12345"})

    # Nur der tunnel_capabilities-Frame geht raus — kein tunnel_credentials mehr.
    non_cap_frames = [m for m in ws_client.sent
                      if m.get("type") != const.MSG_TUNNEL_CAPABILITIES]
    assert non_cap_frames == [], (
        "Kein anderes WS-Frame erwartet (Credentials gehen per REST)"
    )

    # REST-POST an korrekten Endpoint
    assert len(http_session.rest_calls) == 1
    call = http_session.rest_calls[0]
    assert call["method"] == "POST"
    assert "/api/agent/tunnels/abc12345/credentials" in call["url"]
    assert call["json"]["username"] == "ha-fleet-integrator"
    assert call["json"]["password"] == "topsecret"
    assert call["headers"]["X-API-Key"] == "test-api-key-1234567890"
    assert call["headers"]["X-Tunnel-Token"] == "test-token"


@pytest.mark.asyncio
async def test_tunnel_open_ohne_credentials_kein_rest_post(make_forwarder):
    """Ohne Credentials-Objekt soll kein REST-POST versucht werden
    (capabilities-Frame geht trotzdem raus)."""
    fwd, ws_client, http_session, _ = make_forwarder(credentials=None)

    await fwd._on_tunnel_open({"tunnelId": "abc12345"})

    # tunnel_capabilities-Frame geht trotzdem raus, aber sonst nichts.
    non_cap = [m for m in ws_client.sent
               if m.get("type") != const.MSG_TUNNEL_CAPABILITIES]
    assert non_cap == []
    assert http_session.rest_calls == []


@pytest.mark.asyncio
async def test_tunnel_open_mit_user_fehler_kein_rest_post(make_forwarder):
    """Deaktivierter/fehlender User: kein POST, nur Log-Warnung
    (capabilities-Frame geht trotzdem raus)."""
    creds = FakeCredentials(password="", active=False, error="user_disabled")
    fwd, ws_client, http_session, _ = make_forwarder(credentials=creds)

    await fwd._on_tunnel_open({"tunnelId": "deadbeef"})

    non_cap = [m for m in ws_client.sent
               if m.get("type") != const.MSG_TUNNEL_CAPABILITIES]
    assert non_cap == []
    assert http_session.rest_calls == []


@pytest.mark.asyncio
async def test_tunnel_closed_sendet_delete_credentials(make_forwarder):
    """Wenn der Connector die Verbindung trennt, soll DELETE Credentials gesendet werden."""
    rest_calls: list[dict] = []

    def rest_handler(call):
        rest_calls.append(call)
        return _FakeResponse(204, b"", {})

    fwd, _, http_session, hass = make_forwarder(
        credentials=FakeCredentials(password="pw"),
        rest_handler=rest_handler,
    )
    # Slug simulieren (normalerweise durch _on_tunnel_open gesetzt)
    fwd._active_tunnel_slug = "abc12345"

    # Disconnect-Callback manuell triggern
    fwd._on_tunnel_closed()

    # Task abwarten
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    delete_calls = [c for c in http_session.rest_calls if c["method"] == "DELETE"]
    assert len(delete_calls) == 1
    assert "/api/agent/tunnels/abc12345/credentials" in delete_calls[0]["url"]


# --------------------------------------------------------- Tests: HTTP-Forwarding


@pytest.mark.asyncio
async def test_http_request_wird_an_ha_geforwardet_und_response_zurueck(make_forwarder):
    def handler(call):
        assert call["method"] == "GET"
        assert call["url"] == "http://ha.test/lovelace/0"
        assert call["headers"].get("Accept") == "text/html"
        assert "Host" not in call["headers"]
        # Authorization wird durchgereicht (HA-Session-Auth).
        assert call["headers"].get("Authorization") == "Bearer xxx"
        # Plugin ist kein Reverse-Proxy — KEINE X-Forwarded-*-Header an HA.
        assert "X-Forwarded-By" not in call["headers"]
        assert "X-Forwarded-Proto" not in call["headers"]
        assert "X-Forwarded-For" not in call["headers"]
        return _FakeResponse(200, b"<html>ok</html>", {"Content-Type": "text/html"})

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)

    request_frame = {
        "type": const.MSG_TUNNEL_DATA,
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc12345",
        "reqId": "r-1",
        "method": "GET",
        "path": "/lovelace/0",
        "headers": {"Accept": "text/html", "Host": "tun-x", "Authorization": "Bearer xxx"},
        "body": "",
    }
    await fwd._forward(request_frame)

    assert len(ws_client.sent) == 1
    resp = ws_client.sent[0]
    assert resp["type"] == const.MSG_TUNNEL_DATA
    assert resp["kind"] == const.TUNNEL_KIND_HTTP_RESPONSE
    assert resp["tunnelId"] == "abc12345"
    assert resp["reqId"] == "r-1"
    assert resp["status"] == 200
    assert base64.b64decode(resp["body"]) == b"<html>ok</html>"
    assert resp["headers"]["Content-Type"] == "text/html"


@pytest.mark.asyncio
async def test_http_request_mit_body_dekodiert_base64(make_forwarder):
    captured: list[bytes] = []

    def handler(call):
        captured.append(call["data"])
        return _FakeResponse(201, b"", {})

    fwd, _, _, _ = make_forwarder(http_handler=handler)
    body = b'{"state":"on"}'
    request_frame = {
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc",
        "reqId": "r-2",
        "method": "POST",
        "path": "/api/services/light/turn_on",
        "headers": {"Content-Type": "application/json"},
        "body": base64.b64encode(body).decode(),
    }
    await fwd._forward(request_frame)

    assert captured == [body]


@pytest.mark.asyncio
async def test_ha_timeout_liefert_504_an_backend(make_forwarder):
    def handler(call):
        class _Raiser:
            async def __aenter__(self_inner):
                raise asyncio.TimeoutError()

            async def __aexit__(self_inner, *_):
                return False

        return _Raiser()

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)

    await fwd._forward({
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc", "reqId": "r-3",
        "method": "GET", "path": "/", "headers": {}, "body": "",
    })

    resp = ws_client.sent[0]
    assert resp["status"] == 504


@pytest.mark.asyncio
async def test_ha_client_fehler_liefert_502_an_backend(make_forwarder):
    import aiohttp

    def handler(call):
        class _Raiser:
            async def __aenter__(self_inner):
                raise aiohttp.ClientError("connection refused")

            async def __aexit__(self_inner, *_):
                return False

        return _Raiser()

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)

    await fwd._forward({
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc", "reqId": "r-4",
        "method": "GET", "path": "/", "headers": {}, "body": "",
    })

    resp = ws_client.sent[0]
    assert resp["status"] == 502


@pytest.mark.asyncio
async def test_andere_tunnel_data_kinds_werden_ignoriert(make_forwarder):
    fwd, ws_client, _, hass = make_forwarder()

    await fwd._on_tunnel_data({
        "type": const.MSG_TUNNEL_DATA,
        "kind": "live_event",
        "tunnelId": "abc",
    })

    assert hass._tasks == []
    assert ws_client.sent == []


# --------------------------------------------------------- Tests: Header-Filter


def test_header_filter_entfernt_hop_by_hop_proxy_und_xforwarded():
    """Hop-by-hop und Proxy-spezifische Auth werden gefiltert; Cookie/Authorization
    werden BEWUSST durchgereicht (HA-Session-Auth)."""
    raw = {
        "Host": "tun-x.localhost",
        "Connection": "close",
        "Proxy-Authorization": "Basic xxx",
        "Authorization": "Bearer abc",
        "Cookie": "session=def",
        "Accept": "text/html",
        "Content-Type": "application/json",
    }
    out = TunnelForwarder._build_forward_headers(raw)
    assert "Host" not in out
    assert "Connection" not in out
    assert "Proxy-Authorization" not in out
    assert out["Accept"] == "text/html"
    # Cookie + Authorization sollen durchgereicht werden (HA-Login-Session etablieren).
    assert out["Authorization"] == "Bearer abc"
    assert out["Cookie"] == "session=def"
    # Plugin ist kein Reverse-Proxy — KEINE X-Forwarded-*-Header an HA.
    assert "X-Forwarded-By" not in out
    assert "X-Forwarded-Proto" not in out


def test_header_filter_entfernt_eingehende_x_forwarded():
    """X-Forwarded-* Header aus dem Browser-Request muessen rausgefiltert werden,
    damit HAs http-Component den Request nicht als 'from reverse proxy' interpretiert
    und mit 400 ablehnt."""
    raw = {
        "Accept": "text/html",
        "X-Forwarded-For": "203.0.113.42",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "example.com",
        "X-Forwarded-Port": "443",
        "Forwarded": "for=203.0.113.42;proto=https",
    }
    out = TunnelForwarder._build_forward_headers(raw)
    assert out["Accept"] == "text/html"
    assert "X-Forwarded-For" not in out
    assert "X-Forwarded-Proto" not in out
    assert "X-Forwarded-Host" not in out
    assert "X-Forwarded-Port" not in out
    assert "Forwarded" not in out


def test_header_filter_entfernt_origin():
    """Origin-Header muss herausgefiltert werden.

    Browser sendet beim Asset-Load aus der Tunnel-Subdomain
    'Origin: https://tun-XXX.connector.staging.ha-fleet-manager.com'. HA lehnt Requests
    mit fremdem Origin mit 403 ab (CORS-Schutz). Das Plugin reicht den Request
    als lokaler HTTP-Client an localhost:8123 weiter — kein Origin noetig.
    Ohne Origin-Header antwortet HA korrekt mit 200.
    """
    raw = {
        "Accept": "text/javascript",
        "Origin": "https://tun-abc123.connector.staging.ha-fleet-manager.com",
        "Cookie": "session=xyz",
    }
    out = TunnelForwarder._build_forward_headers(raw)
    assert out["Accept"] == "text/javascript"
    assert "Origin" not in out
    # Cookie wird weiterhin durchgereicht (HA-Session-Auth)
    assert out["Cookie"] == "session=xyz"


def test_header_filter_entfernt_origin_unabhaengig_von_sec_fetch():
    """Origin wird auch dann gefiltert, wenn Sec-Fetch-* Header gesetzt sind.

    Sicherstellt, dass Origin IMMER herausgefiltert wird — unabhaengig davon,
    welche Browser-spezifischen Begleit-Header gesetzt sind.
    """
    raw = {
        "Accept": "text/html",
        "Origin": "https://tun-ff220982.connector.staging.ha-fleet-manager.com",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "script",
        "User-Agent": "Mozilla/5.0",
    }
    out = TunnelForwarder._build_forward_headers(raw)
    assert "Origin" not in out
    assert out["Accept"] == "text/html"
    # Sec-Fetch-* und User-Agent werden NICHT gefiltert (kein Schaden fuer HA)
    assert "Sec-Fetch-Site" in out
    assert "User-Agent" in out


# --------------------------------------------------------- Tests: Endkunden-Abbruch (§4.4)


@pytest.mark.asyncio
async def test_async_close_tunnel_ohne_aktiven_tunnel(make_forwarder):
    """Ohne aktiven Tunnel ist async_close_tunnel ein No-Op."""
    fwd, ws_client, _, _ = make_forwarder()

    result = await fwd.async_close_tunnel()

    assert result is False
    assert ws_client.disconnect_calls == 0


@pytest.mark.asyncio
async def test_async_close_tunnel_triggert_disconnect_und_callback(make_forwarder):
    """Mit aktivem Tunnel: disconnect() wird gerufen, on_close-Callback feuert."""
    close_calls: list[str] = []

    async def on_close():
        close_calls.append("called")

    ws_client = FakeWsClient()
    ws_client.is_connected = True
    user = FakeIntegratorUser(FakeCredentials())
    hass = FakeHass()
    http_session = FakeHttpSession()

    fwd = TunnelForwarder(
        hass,
        ws_client,
        user,
        backend_url="https://api.ha-fleet-manager.com",
        api_key="test-api-key-1234567890",
        http_session=http_session,
        on_close=on_close,
    )
    fwd._active_tunnel_slug = "abc12345"

    result = await fwd.async_close_tunnel()

    # Tasks abwarten (delete_credentials + on_close)
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    assert result is True
    assert ws_client.disconnect_calls == 1
    # disconnect() im FakeWsClient feuert den registrierten Disconnect-Callback,
    # der wiederum _on_tunnel_closed im Forwarder triggert.
    assert close_calls == ["called"]
    # DELETE-Credentials wurde dabei abgesetzt
    delete_calls = [c for c in http_session.rest_calls if c["method"] == "DELETE"]
    assert len(delete_calls) == 1


@pytest.mark.asyncio
async def test_tunnel_open_feuert_signal_state_true(make_forwarder, monkeypatch):
    """tunnel_open dispatcht SIGNAL_TUNNEL_STATE mit True."""
    dispatched: list[tuple] = []

    def fake_dispatch(_hass, signal, *args):
        dispatched.append((signal, args))

    from ha_fleet_agent import tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "async_dispatcher_send", fake_dispatch)

    fwd, _, _, _ = make_forwarder(credentials=FakeCredentials())
    await fwd._on_tunnel_open({"tunnelId": "abc12345"})

    tunnel_signals = [
        s for s in dispatched if s[0] == const.SIGNAL_TUNNEL_STATE
    ]
    assert len(tunnel_signals) == 1
    assert tunnel_signals[0][1] == ("", True)  # entry_id default "", open=True


@pytest.mark.asyncio
async def test_tunnel_closed_feuert_signal_state_false(make_forwarder, monkeypatch):
    """_on_tunnel_closed dispatcht SIGNAL_TUNNEL_STATE mit False + ruft on_close."""
    dispatched: list[tuple] = []

    def fake_dispatch(_hass, signal, *args):
        dispatched.append((signal, args))

    from ha_fleet_agent import tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "async_dispatcher_send", fake_dispatch)

    fwd, _, _, hass = make_forwarder()
    fwd._active_tunnel_slug = "abc12345"
    fwd._on_tunnel_closed()
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    tunnel_signals = [
        s for s in dispatched if s[0] == const.SIGNAL_TUNNEL_STATE
    ]
    assert len(tunnel_signals) == 1
    assert tunnel_signals[0][1] == ("", False)


def test_response_header_filter_entfernt_content_length_und_encoding():
    raw = {
        "Content-Length": "42",
        "Content-Encoding": "gzip",
        "Transfer-Encoding": "chunked",
        "X-Custom": "yes",
    }
    out = TunnelForwarder._strip_response_headers(raw)
    assert "Content-Length" not in out
    assert "Content-Encoding" not in out
    assert "Transfer-Encoding" not in out
    assert out["X-Custom"] == "yes"


# --------------------------------------------------------- Tests: Chunked Response (>64 KiB → Connector-WS-Limit)
#
# Hintergrund: Quarkus WebSockets Next hat ein Default-`max-frame-size` von 64 KiB.
# HA-Asset-Responses (z.B. /frontend_latest/core.*.js) können 1–2 MB gross sein —
# in einem Frame würde der Connector die Verbindung mit
# CorruptedWebSocketFrameException hart schliessen.
# Plugin 0.4.3 splittet darum Response-Bodies in 32-KiB-Chunks (`TUNNEL_CHUNK_SIZE_BYTES`):
#   - Frame 1: kind=http_response, status+headers+erstes Body-Stück, "more": true (falls weitere folgen)
#   - Frame 2..n-1: kind=http_response_body, nur body, "more": true
#   - Frame n (letzter): kind=http_response_body (oder kind=http_response wenn n=1), kein "more"-Feld
# Bei ≤32 KiB Body wird genau 1 Frame ohne "more"-Feld gesendet (kein leerer Trailer).


def _assemble_chunked_body(frames: list[dict[str, Any]]) -> bytes:
    """Hilfsfunktion: Konkateniert die Body-Chunks aus einer Frame-Sequenz."""
    parts: list[bytes] = []
    for f in frames:
        body_b64 = f.get("body", "")
        parts.append(base64.b64decode(body_b64) if body_b64 else b"")
    return b"".join(parts)


@pytest.mark.asyncio
async def test_chunked_small_response_unter_chunk_size_ein_frame_kein_more(make_forwarder):
    """Body < 32 KiB → genau 1 Frame, kein 'more'-Feld, kind=http_response."""
    body = b"x" * 10_000  # 10 KB — deutlich unter Chunk-Size

    def handler(call):
        return _FakeResponse(200, body, {"Content-Type": "application/octet-stream"})

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)
    await fwd._forward({
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc12345", "reqId": "r-small",
        "method": "GET", "path": "/asset.bin", "headers": {}, "body": "",
    })

    assert len(ws_client.sent) == 1, "Body unter Chunk-Size → genau 1 Frame"
    frame = ws_client.sent[0]
    assert frame["kind"] == const.TUNNEL_KIND_HTTP_RESPONSE
    assert frame["status"] == 200
    assert frame["reqId"] == "r-small"
    assert "headers" in frame
    assert "more" not in frame, "Single-Frame-Response trägt kein 'more'-Feld"
    assert base64.b64decode(frame["body"]) == body


@pytest.mark.asyncio
async def test_chunked_large_response_mehrere_frames_letzter_ohne_more(make_forwarder):
    """Body 1 MB → 32 Frames (1 MB / 32 KiB), letzter ohne 'more', alle anderen 'more': true."""
    chunk_size = 32 * 1024  # muss mit TUNNEL_CHUNK_SIZE_BYTES übereinstimmen
    total = 1 * 1024 * 1024  # 1 MB
    body = bytes((i % 251) for i in range(total))  # deterministisches Muster

    def handler(call):
        return _FakeResponse(200, body, {"Content-Type": "application/javascript"})

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)
    await fwd._forward({
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc12345", "reqId": "r-big",
        "method": "GET", "path": "/frontend_latest/core.js", "headers": {}, "body": "",
    })

    expected_frames = (total + chunk_size - 1) // chunk_size
    assert len(ws_client.sent) == expected_frames, (
        f"1 MB Body sollte in {expected_frames} Frames aufgeteilt werden"
    )

    first = ws_client.sent[0]
    middle = ws_client.sent[1:-1]
    last = ws_client.sent[-1]

    # Frame 1: trägt status + headers + kind=http_response + more=true
    assert first["kind"] == const.TUNNEL_KIND_HTTP_RESPONSE
    assert first["status"] == 200
    assert first["headers"]["Content-Type"] == "application/javascript"
    assert first["reqId"] == "r-big"
    assert first["tunnelId"] == "abc12345"
    assert first.get("more") is True
    assert len(base64.b64decode(first["body"])) == chunk_size

    # Mittlere Frames: kind=http_response_body, keine headers, more=true
    for f in middle:
        assert f["kind"] == "http_response_body"
        assert f["reqId"] == "r-big"
        assert f["tunnelId"] == "abc12345"
        assert "headers" not in f, "Folge-Frames dürfen keine headers tragen"
        assert "status" not in f, "Folge-Frames dürfen keinen status tragen"
        assert f.get("more") is True
        assert len(base64.b64decode(f["body"])) == chunk_size

    # Letzter Frame: kind=http_response_body, KEIN 'more'-Feld → signalisiert Ende
    assert last["kind"] == "http_response_body"
    assert "more" not in last, "Letzter Frame darf kein 'more'-Feld tragen"
    assert "headers" not in last
    last_len = len(base64.b64decode(last["body"]))
    assert 0 < last_len <= chunk_size

    # Konkatenierter Body muss exakt dem Original entsprechen
    assert _assemble_chunked_body(ws_client.sent) == body


@pytest.mark.asyncio
async def test_chunked_exact_boundary_ein_frame_ohne_trailing_chunk(make_forwarder):
    """Body == 32 KiB → 1 Frame, kein leerer Trailing-Chunk."""
    chunk_size = 32 * 1024
    body = b"A" * chunk_size  # exakt an der Chunk-Grenze

    def handler(call):
        return _FakeResponse(200, body, {"Content-Type": "text/plain"})

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)
    await fwd._forward({
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc12345", "reqId": "r-edge",
        "method": "GET", "path": "/boundary", "headers": {}, "body": "",
    })

    assert len(ws_client.sent) == 1, (
        "Body genau an der Chunk-Grenze → 1 Frame, kein leerer Trailer"
    )
    frame = ws_client.sent[0]
    assert frame["kind"] == const.TUNNEL_KIND_HTTP_RESPONSE
    assert frame["status"] == 200
    assert "more" not in frame
    assert base64.b64decode(frame["body"]) == body


@pytest.mark.asyncio
async def test_chunked_empty_body_ein_frame_mit_leerem_body(make_forwarder):
    """Empty Body → 1 Frame mit body='', kein 'more'-Feld (z.B. 204 No Content)."""

    def handler(call):
        return _FakeResponse(204, b"", {})

    fwd, ws_client, _, _ = make_forwarder(http_handler=handler)
    await fwd._forward({
        "kind": const.TUNNEL_KIND_HTTP_REQUEST,
        "tunnelId": "abc12345", "reqId": "r-empty",
        "method": "DELETE", "path": "/api/whatever", "headers": {}, "body": "",
    })

    assert len(ws_client.sent) == 1
    frame = ws_client.sent[0]
    assert frame["kind"] == const.TUNNEL_KIND_HTTP_RESPONSE
    assert frame["status"] == 204
    assert frame["body"] == ""
    assert "more" not in frame


# --------------------------------------------------------- Tests: WebSocket-Tunneling (0.5.0)
#
# Plugin meldet beim tunnel_open seine Capabilities; auf ws_open vom Connector
# baut es per aiohttp.ws_connect eine eigene WS-Verbindung zu HA auf und pumpt
# Frames bidirektional. Tests prüfen Capabilities-Frame, ws_open/ws_accepted-
# Choreographie, Pump-Richtungen (HA→Connector und Connector→HA), Binary-Base64,
# Chunking, ws_close-Pfade und Aufraeumen beim Tunnel-Disconnect.


@pytest.mark.asyncio
async def test_plugin_meldet_capabilities_beim_tunnel_open(make_forwarder):
    """tunnel_open: Plugin sendet zuerst tunnel_capabilities-Frame mit
    http_chunked + ws_tunnel an den Connector."""
    fwd, ws_client, _, _ = make_forwarder(credentials=FakeCredentials())

    await fwd._on_tunnel_open({"tunnelId": "abc12345"})

    caps = [m for m in ws_client.sent
            if m.get("type") == const.MSG_TUNNEL_CAPABILITIES]
    assert len(caps) == 1, "Genau ein tunnel_capabilities-Frame erwartet"
    assert caps[0]["tunnelId"] == "abc12345"
    assert const.PLUGIN_CAPABILITY_HTTP_CHUNKED in caps[0]["capabilities"]
    assert const.PLUGIN_CAPABILITY_WS_TUNNEL in caps[0]["capabilities"]


@pytest.mark.asyncio
async def test_forwarder_erkennt_ws_open_und_oeffnet_aiohttp_ws_zu_ha(make_forwarder):
    """ws_open: Plugin ruft session.ws_connect mit der korrekten ws://-URL +
    gefilterten Headern auf."""
    async def ws_handler(call):
        return FakeHaWs()

    fwd, _, http_session, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345",
        "wsId": "w-aaaaaaaa",
        "path": "/api/websocket",
        "headers": {
            "Cookie": "auth=xyz",
            "Origin": "https://tun-x.connector.example.com",  # muss gefiltert werden
            "Sec-WebSocket-Version": "13",  # muss gefiltert werden
        },
    })

    assert len(http_session.ws_calls) == 1
    call = http_session.ws_calls[0]
    # http://ha.test → ws://ha.test
    assert call["url"] == "ws://ha.test/api/websocket"
    # Cookie ist durchgereicht
    assert call["headers"].get("Cookie") == "auth=xyz"
    # Origin ist gefiltert (CORS-Trigger bei HA)
    assert "Origin" not in call["headers"]
    # Sec-WebSocket-Version ist gefiltert (aiohttp generiert eigenes)
    assert "Sec-WebSocket-Version" not in call["headers"]


@pytest.mark.asyncio
async def test_forwarder_sendet_ws_accepted_bei_erfolgreichem_ha_upgrade(make_forwarder):
    """ws_open erfolgreich → Plugin sendet ws_accepted-Frame an Connector."""
    async def ws_handler(call):
        return FakeHaWs()

    fwd, ws_client, _, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345",
        "wsId": "w-aaaaaaaa",
        "path": "/api/websocket",
        "headers": {},
    })

    accepted = [m for m in ws_client.sent
                if m.get("kind") == const.TUNNEL_KIND_WS_ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0]["tunnelId"] == "abc12345"
    assert accepted[0]["wsId"] == "w-aaaaaaaa"


@pytest.mark.asyncio
async def test_forwarder_pumpt_text_message_von_ha_an_connector(make_forwarder):
    """HA → Plugin → Connector: TEXT-Frame landet als ws_message text bei Connector."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, ws_client, _, hass = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })
    # Frame in HA-WS einspielen + Close, damit der Pump-Task endet
    ha_ws.push_text('{"id":1,"type":"auth_ok"}')
    ha_ws.push_close(1000)

    # Warten bis Pump fertig ist
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    messages = [m for m in ws_client.sent
                if m.get("kind") == const.TUNNEL_KIND_WS_MESSAGE]
    assert len(messages) == 1, f"Genau 1 ws_message erwartet, sent={ws_client.sent}"
    assert messages[0]["opcode"] == const.WS_OPCODE_TEXT
    assert messages[0]["payload"] == '{"id":1,"type":"auth_ok"}'
    assert "more" not in messages[0]


@pytest.mark.asyncio
async def test_forwarder_pumpt_text_message_von_connector_an_ha(make_forwarder):
    """Connector → Plugin → HA: ws_message-Frame landet als send_str() an HA-WS."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, _, _, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })

    await fwd._relay_ws_message_to_ha({
        "kind": const.TUNNEL_KIND_WS_MESSAGE,
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "opcode": const.WS_OPCODE_TEXT,
        "payload": '{"type":"subscribe_events"}',
    })

    assert ha_ws.sent_text == ['{"type":"subscribe_events"}']
    assert ha_ws.sent_bytes == []


@pytest.mark.asyncio
async def test_forwarder_pumpt_binary_als_base64(make_forwarder):
    """HA → Plugin → Connector: BINARY-Frame wird base64-encoded uebertragen."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, ws_client, _, hass = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })
    binary_payload = bytes([0x01, 0x02, 0x03, 0xff])
    ha_ws.push_binary(binary_payload)
    ha_ws.push_close(1000)
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    msg = next(m for m in ws_client.sent
               if m.get("kind") == const.TUNNEL_KIND_WS_MESSAGE)
    assert msg["opcode"] == const.WS_OPCODE_BINARY
    assert base64.b64decode(msg["payload"]) == binary_payload


@pytest.mark.asyncio
async def test_forwarder_chunked_grosse_messages(make_forwarder):
    """HA sendet einen Frame >32 KiB → Plugin chunkt ihn in mehrere ws_message-
    Frames; letzter ohne 'more'-Feld."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, ws_client, _, hass = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })
    big_text = "A" * (const.WS_CHUNK_SIZE_BYTES * 3 + 10)  # 3 volle Chunks + Rest
    ha_ws.push_text(big_text)
    ha_ws.push_close(1000)
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    msgs = [m for m in ws_client.sent
            if m.get("kind") == const.TUNNEL_KIND_WS_MESSAGE]
    assert len(msgs) == 4, f"3 volle + 1 Rest-Chunk erwartet, got {len(msgs)}"
    assert all(m.get("more") is True for m in msgs[:-1]), \
        "Alle bis auf den letzten muessen 'more': true tragen"
    assert "more" not in msgs[-1], "Letzter Chunk darf kein 'more'-Feld tragen"
    # Wieder zusammensetzen
    reassembled = "".join(m["payload"] for m in msgs)
    assert reassembled == big_text


@pytest.mark.asyncio
async def test_ws_close_von_connector_schliesst_ha_seite(make_forwarder):
    """Connector → Plugin ws_close: Plugin schliesst HA-WS und entfernt sie aus
    der Map."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, _, _, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })
    assert "w-aaaaaaaa" in fwd._ha_ws

    await fwd._close_ws_from_connector({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "code": 1000, "reason": "normal closure",
    })

    assert ha_ws.closed
    assert ha_ws.close_code == 1000
    assert "w-aaaaaaaa" not in fwd._ha_ws


@pytest.mark.asyncio
async def test_ws_close_von_ha_schliesst_connector_seite(make_forwarder):
    """HA schliesst die WS → Pump-Task endet → Plugin sendet ws_close an Connector."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, ws_client, _, hass = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })
    ha_ws.push_close(1000)
    await asyncio.gather(*hass._tasks, return_exceptions=True)

    closes = [m for m in ws_client.sent
              if m.get("kind") == const.TUNNEL_KIND_WS_CLOSE]
    assert len(closes) == 1, f"ws_close erwartet, sent={ws_client.sent}"
    assert closes[0]["tunnelId"] == "abc12345"
    assert closes[0]["wsId"] == "w-aaaaaaaa"
    assert closes[0]["code"] == 1000


@pytest.mark.asyncio
async def test_tunnel_disconnect_schliesst_alle_offenen_ws_sessions(make_forwarder):
    """async_shutdown: alle aktiven HA-WS werden geschlossen, Pumps gecancelt."""
    ha_ws_1 = FakeHaWs()
    ha_ws_2 = FakeHaWs()
    handlers_returned = [ha_ws_1, ha_ws_2]

    async def ws_handler(call):
        return handlers_returned.pop(0)

    fwd, _, _, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-1",
        "path": "/api/websocket", "headers": {},
    })
    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-2",
        "path": "/api/websocket", "headers": {},
    })
    assert len(fwd._ha_ws) == 2

    await fwd.async_shutdown()

    assert ha_ws_1.closed
    assert ha_ws_2.closed
    assert fwd._ha_ws == {}
    assert fwd._ws_pump_tasks == {}


@pytest.mark.asyncio
async def test_ha_ws_upgrade_failed_sendet_ws_close_an_connector(make_forwarder):
    """ws_connect wirft Exception (z.B. HA antwortet 400) → Plugin sendet
    ws_close an Connector mit Code 1011."""
    async def ws_handler(call):
        raise RuntimeError("HA Handshake fehlgeschlagen")

    fwd, ws_client, _, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })

    closes = [m for m in ws_client.sent
              if m.get("kind") == const.TUNNEL_KIND_WS_CLOSE]
    assert len(closes) == 1
    assert closes[0]["wsId"] == "w-aaaaaaaa"
    assert closes[0]["code"] == 1011
    # Kein ws_accepted vorher
    assert not any(m.get("kind") == const.TUNNEL_KIND_WS_ACCEPTED
                   for m in ws_client.sent)


@pytest.mark.asyncio
async def test_relay_chunked_message_von_connector_an_ha_reassembliert(make_forwarder):
    """Connector → Plugin → HA: gechunkte ws_message (more=true) werden
    reassembliert und erst beim letzten Chunk an HA gesendet."""
    ha_ws = FakeHaWs()

    async def ws_handler(call):
        return ha_ws

    fwd, _, _, _ = make_forwarder(ws_handler=ws_handler)

    await fwd._open_ws_to_ha({
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "path": "/api/websocket", "headers": {},
    })

    # Drei Chunks: 'AAA', 'BBB', 'CCC' — nur der letzte ohne 'more'.
    await fwd._relay_ws_message_to_ha({
        "kind": const.TUNNEL_KIND_WS_MESSAGE,
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "opcode": const.WS_OPCODE_TEXT,
        "payload": "AAA", "more": True,
    })
    await fwd._relay_ws_message_to_ha({
        "kind": const.TUNNEL_KIND_WS_MESSAGE,
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "opcode": const.WS_OPCODE_TEXT,
        "payload": "BBB", "more": True,
    })
    # Nach 2 Chunks: noch nichts an HA gesendet
    assert ha_ws.sent_text == []

    await fwd._relay_ws_message_to_ha({
        "kind": const.TUNNEL_KIND_WS_MESSAGE,
        "tunnelId": "abc12345", "wsId": "w-aaaaaaaa",
        "opcode": const.WS_OPCODE_TEXT,
        "payload": "CCC",  # kein 'more'
    })

    assert ha_ws.sent_text == ["AAABBBCCC"]
