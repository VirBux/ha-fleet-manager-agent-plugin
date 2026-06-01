"""Tests fuer IntegratorUserManager — Fokus auf den HA-Auth-Provider-Lookup.

Hintergrund: In aktuellen HA-Versionen heisst die Methode `get_auth_provider`
(synchron, @callback), nicht `async_get_provider`. Dieser Test sichert ab,
dass der korrekte Methodenname verwendet wird — sonst gibt es beim Setup
einen `AttributeError` (siehe Bug-Report 2026-05-24).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_fleet_agent.integrator_user import IntegratorUserManager


class _FakeAuthProviderData:
    def __init__(self) -> None:
        self.added: list[tuple[str, str]] = []
        self.saved = False

    def add_auth(self, username: str, password: str) -> None:
        self.added.append((username, password))

    async def async_save(self) -> None:
        self.saved = True


class _FakeAuthProvider:
    def __init__(self) -> None:
        self.data = _FakeAuthProviderData()
        self._creds: dict[str, Any] = {}

    async def async_get_or_create_credentials(self, payload: dict[str, str]) -> Any:
        return {"username": payload["username"]}


class _FakeAuthManager:
    """Imitiert hass.auth — exponiert die echte API (`get_auth_provider`, synchron)."""

    def __init__(self, provider: _FakeAuthProvider) -> None:
        self._provider = provider
        self.created_users: list[dict[str, Any]] = []

    # synchron, @callback in HA — hier reicht eine normale Methode
    def get_auth_provider(self, provider_type: str, provider_id: str | None) -> Any:
        if provider_type == "homeassistant":
            return self._provider
        return None

    async def async_create_user(self, **kwargs) -> Any:
        self.created_users.append(kwargs)
        user = MagicMock()
        user.id = "fake-user-id"
        user.is_active = True
        return user

    async def async_link_user(self, *_args, **_kwargs) -> None:
        return None

    async def async_get_user(self, user_id: str) -> Any:
        return None


class _FakeHass:
    def __init__(self, provider: _FakeAuthProvider) -> None:
        self.auth = _FakeAuthManager(provider)

    async def async_add_executor_job(self, func, *args):
        # Im Test: synchron ausfuehren statt in Executor
        func(*args)


@pytest.mark.asyncio
async def test_create_user_nutzt_get_auth_provider_nicht_async_get_provider():
    """Regressions-Test: der alte Methodenname `async_get_provider` existiert
    in aktuellen HA-Versionen nicht mehr — der Code muss `get_auth_provider`
    verwenden."""

    provider = _FakeAuthProvider()
    hass = _FakeHass(provider)
    manager = IntegratorUserManager(hass, entry_id="test-entry")

    # `async_setup` faellt durch zur User-Erzeugung, weil Storage leer ist.
    await manager.async_setup()

    # Wenn der Code `async_get_provider` aufgerufen haette, waere AttributeError
    # gefallen (FakeAuthManager hat die Methode nicht).
    assert manager.credentials is not None
    assert manager.credentials.username == "ha-fleet-integrator"
    assert manager.credentials.password  # nicht leer
    assert provider.data.added, "add_auth wurde nicht aufgerufen"
    assert provider.data.saved, "async_save wurde nicht aufgerufen"


@pytest.mark.asyncio
async def test_create_user_wirft_bei_fehlendem_provider():
    """Ohne den 'homeassistant'-Provider muss eine sprechende Exception fliegen."""

    class _NoProviderHass(_FakeHass):
        def __init__(self) -> None:
            super().__init__(_FakeAuthProvider())
            self.auth.get_auth_provider = lambda *a, **kw: None  # type: ignore[assignment]

    manager = IntegratorUserManager(_NoProviderHass(), entry_id="test-entry")
    with pytest.raises(RuntimeError, match="homeassistant.*Auth-Provider"):
        await manager.async_setup()


# Fallback fuer pytest-asyncio < 0.23: pytest-Mark muss als config-flag erkannt sein.
# Wir registrieren ihn manuell, damit der Test auch ohne pytest-asyncio durchlaeuft
# (run_asyncio-Helper unten).
def _maybe_run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_create_user_nutzt_get_auth_provider_sync_fallback():
    """Fallback ohne pytest-asyncio: ruft die async-Tests synchron auf."""
    try:
        import pytest_asyncio  # noqa: F401
        return  # pytest-asyncio uebernimmt
    except ImportError:
        pass
    provider = _FakeAuthProvider()
    hass = _FakeHass(provider)
    manager = IntegratorUserManager(hass, entry_id="test-entry")
    _maybe_run(manager.async_setup())
    assert manager.credentials is not None
