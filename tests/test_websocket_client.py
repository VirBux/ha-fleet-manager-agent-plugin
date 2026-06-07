"""Tests fuer FleetWebSocketClient.connect_for_tunnel — URL-Konstruktion (#108 Phase D)."""

from __future__ import annotations

import asyncio

import pytest

from ha_fleet_agent.websocket_client import FleetWebSocketClient


class FakeWs:
    """Minimaler ClientWebSocketResponse-Stub — leer, read_loop endet sofort."""

    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.ws_connect_url: str | None = None

    async def ws_connect(self, url, **kwargs):
        self.ws_connect_url = url
        return FakeWs()


class FakeHass:
    def __init__(self):
        self.tasks: list[asyncio.Task] = []

    def async_create_background_task(self, coro, name=None):
        task = asyncio.get_event_loop().create_task(coro)
        self.tasks.append(task)
        return task

    async def async_add_executor_job(self, func, *args):
        return func(*args)


async def _connect(slug=None):
    hass = FakeHass()
    session = FakeSession()
    client = FleetWebSocketClient(hass, "e1", session)
    # ws:// statt wss:// → kein SSL-Context-Pfad.
    await client.connect_for_tunnel("tok-abc", "ws://relay.test/ws/agent", slug=slug)
    await asyncio.gather(*hass.tasks, return_exceptions=True)  # read_loop aufräumen
    return session.ws_connect_url


@pytest.mark.asyncio
async def test_connect_haengt_token_und_slug_an_url():
    url = await _connect(slug="deadbeef")
    assert "token=tok-abc" in url
    assert "slug=deadbeef" in url


@pytest.mark.asyncio
async def test_connect_ohne_slug_keine_slug_query():
    url = await _connect(slug=None)
    assert "token=tok-abc" in url
    assert "slug=" not in url


@pytest.mark.asyncio
async def test_connect_guard_verhindert_parallelen_aufbau():
    """#108: Ein zweiter connect_for_tunnel, während der erste noch in ws_connect
    hängt (self._ws noch None), darf KEINE zweite WS öffnen."""

    class SlowSession:
        def __init__(self):
            self.reached = asyncio.Event()
            self.release = asyncio.Event()
            self.count = 0

        async def ws_connect(self, url, **kwargs):
            self.count += 1
            self.reached.set()
            await self.release.wait()
            return FakeWs()

    hass = FakeHass()
    session = SlowSession()
    client = FleetWebSocketClient(hass, "e1", session)

    t1 = asyncio.get_event_loop().create_task(
        client.connect_for_tunnel("tok-abc", "ws://relay.test/ws/agent")
    )
    await session.reached.wait()  # erster Aufbau hängt jetzt in ws_connect

    # Zweiter Aufruf während _connecting=True → muss ignoriert werden.
    await client.connect_for_tunnel("tok-abc", "ws://relay.test/ws/agent")
    assert session.count == 1, "Zweiter paralleler connect darf keine zweite WS öffnen"

    session.release.set()
    await t1
    await asyncio.gather(*hass.tasks, return_exceptions=True)
    assert session.count == 1


@pytest.mark.asyncio
async def test_connect_slug_nicht_doppelt_wenn_schon_in_url():
    hass = FakeHass()
    session = FakeSession()
    client = FleetWebSocketClient(hass, "e1", session)
    await client.connect_for_tunnel(
        "tok-abc", "ws://relay.test/ws/agent?slug=cafef00d", slug="deadbeef"
    )
    await asyncio.gather(*hass.tasks, return_exceptions=True)
    # Vorhandener slug-Param bleibt; kein zweiter wird angehängt.
    assert session.ws_connect_url.count("slug=") == 1
    assert "slug=cafef00d" in session.ws_connect_url
