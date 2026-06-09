"""Tests fuer IntegratorUserManager — HA-Auth-Provider-Lookup + Idempotenz.

Zwei Themen:

1. In aktuellen HA-Versionen heisst die Methode `get_auth_provider` (synchron,
   @callback), nicht `async_get_provider`. Ein Test sichert den Methodennamen
   ab — sonst gibt es beim Setup einen `AttributeError` (Bug-Report 2026-05-24).

2. Ist der Integrator-Username im `homeassistant`-Auth-Provider bereits bekannt
   (z.B. nach einem HACS-Update mit verlorenem lokalen Store), darf `_create_user`
   **nicht** blind `add_auth` aufrufen — das wirft `InvalidUsername:
   username_already_exists` (Bug-Report 2026-06-04). Stattdessen wird der
   bestehende Login uebernommen und nur das Passwort rotiert.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ha_fleet_agent.const import INTEGRATOR_USERNAME
from ha_fleet_agent.integrator_user import IntegratorUserManager


class _FakeCredentials:
    """Imitiert ein HA-`Credentials`-Objekt (nur die genutzten Felder)."""

    def __init__(self, username: str, cred_id: str) -> None:
        self.data = {"username": username}
        self.id = cred_id


class _FakeUser:
    def __init__(self, user_id: str) -> None:
        self.id = user_id
        self.is_active = True
        # token_id -> Refresh-Token (fuer #110 async_deactivate-Tests)
        self.refresh_tokens: dict[str, Any] = {}


class _FakeAuthProviderData:
    """Bildet `homeassistant`-Provider-`Data` ab: users-Liste + add/change_password.

    `add_auth` wirft bei einem bereits bekannten (normalisierten) Username —
    genau wie HAs echtes `InvalidUsername`. Damit beweisen die Reuse-Tests, dass
    der Produktionscode `add_auth` in dem Fall gar nicht erst aufruft.
    """

    def __init__(self, existing_usernames: tuple[str, ...] = ()) -> None:
        self.users: list[dict[str, str]] = [
            {"username": u, "password": "old-hash"} for u in existing_usernames
        ]
        self.added: list[tuple[str, str]] = []
        self.changed: list[tuple[str, str]] = []
        self.saved = False

    def normalize_username(self, username: str, *, force_normalize: bool = False) -> str:
        # HA: strip + casefold. INTEGRATOR_USERNAME ist bereits lowercase.
        return username.strip().casefold()

    def _index(self, username: str) -> int | None:
        norm = self.normalize_username(username)
        for i, entry in enumerate(self.users):
            if self.normalize_username(entry["username"]) == norm:
                return i
        return None

    def add_auth(self, username: str, password: str) -> None:
        if self._index(username) is not None:
            raise ValueError("username_already_exists")  # wie HAs InvalidUsername
        self.users.append({"username": username, "password": password})
        self.added.append((username, password))

    def change_password(self, username: str, new_password: str) -> None:
        idx = self._index(username)
        if idx is None:
            raise ValueError("user_not_found")  # wie HAs InvalidUser
        self.users[idx]["password"] = new_password
        self.changed.append((username, new_password))

    async def async_save(self) -> None:
        self.saved = True


class _FakeAuthProvider:
    def __init__(self, data: _FakeAuthProviderData | None = None) -> None:
        self.data = data if data is not None else _FakeAuthProviderData()
        self._cred_seq = 0
        # Vorab verknuepfte Credential (Reuse-Szenario mit lebendem User).
        self._linked_cred: _FakeCredentials | None = None

    def _prelink(self, username: str) -> _FakeCredentials:
        """Test-Helfer: simuliert eine bereits existierende, verknuepfte Credential."""
        self._cred_seq += 1
        self._linked_cred = _FakeCredentials(username, f"cred-{self._cred_seq}")
        return self._linked_cred

    async def async_get_or_create_credentials(self, payload: dict[str, str]) -> Any:
        # HA gibt die vorhandene (verknuepfte) Credential zurueck, sonst eine neue.
        if self._linked_cred is not None:
            return self._linked_cred
        self._cred_seq += 1
        return _FakeCredentials(payload["username"], f"cred-new-{self._cred_seq}")


class _FakeAuthManager:
    """Imitiert hass.auth — exponiert die echte API (`get_auth_provider`, synchron)."""

    def __init__(self, provider: _FakeAuthProvider) -> None:
        self._provider = provider
        self.created_users: list[dict[str, Any]] = []
        self.linked: list[tuple[str, str]] = []
        self._user_by_cred_id: dict[str, _FakeUser] = {}
        self._users_by_id: dict[str, _FakeUser] = {}  # #110: async_get_user
        self.updated: list[tuple[str, dict[str, Any]]] = []  # #110: async_update_user
        self.removed_tokens: list[Any] = []  # #110: async_remove_refresh_token
        self._user_seq = 0

    # synchron, @callback in HA — hier reicht eine normale Methode
    def get_auth_provider(self, provider_type: str, provider_id: str | None) -> Any:
        if provider_type == "homeassistant":
            return self._provider
        return None

    async def async_create_user(self, **kwargs) -> Any:
        self._user_seq += 1
        self.created_users.append(kwargs)
        user = _FakeUser(f"new-user-{self._user_seq}")
        self._users_by_id[user.id] = user
        return user

    async def async_link_user(self, user: Any, credentials: Any) -> None:
        self.linked.append((user.id, credentials.id))
        self._user_by_cred_id[credentials.id] = user
        self._users_by_id[user.id] = user

    async def async_get_user(self, user_id: str) -> Any:
        return self._users_by_id.get(user_id)

    async def async_update_user(self, user: Any, **kwargs: Any) -> None:
        # HA-Signatur: async_update_user(user, name=None, is_active=None, ...)
        if kwargs.get("is_active") is not None:
            user.is_active = kwargs["is_active"]
        self.updated.append((user.id, kwargs))

    async def async_remove_refresh_token(self, token: Any) -> None:
        self.removed_tokens.append(token)
        for user in self._users_by_id.values():
            for tid in [t for t, val in user.refresh_tokens.items() if val is token]:
                del user.refresh_tokens[tid]

    async def async_get_user_by_credentials(self, credentials: Any) -> Any:
        return self._user_by_cred_id.get(credentials.id)

    def _preexisting_user(self, credentials: _FakeCredentials) -> _FakeUser:
        """Test-Helfer: verknuepft eine Credential vorab mit einem User."""
        self._user_seq += 1
        user = _FakeUser(f"existing-user-{self._user_seq}")
        self._user_by_cred_id[credentials.id] = user
        self._users_by_id[user.id] = user
        return user


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
    assert manager.credentials.username == INTEGRATOR_USERNAME
    assert manager.credentials.password  # nicht leer
    assert provider.data.added, "add_auth wurde nicht aufgerufen"
    assert provider.data.saved, "async_save wurde nicht aufgerufen"
    assert len(hass.auth.created_users) == 1, "genau ein User bei Erstinstallation"


@pytest.mark.asyncio
async def test_create_user_uebernimmt_bestehenden_login_ohne_add_auth():
    """HACS-Update-Regression (2026-06-04): existiert der Integrator-Auth-Eintrag
    bereits und ist ein HA-User verknuepft, darf KEIN zweiter User entstehen und
    `add_auth` NICHT aufgerufen werden (sonst `username_already_exists`).
    Stattdessen wird der bestehende User uebernommen und das Passwort rotiert."""

    data = _FakeAuthProviderData(existing_usernames=(INTEGRATOR_USERNAME,))
    provider = _FakeAuthProvider(data)
    cred = provider._prelink(INTEGRATOR_USERNAME)
    hass = _FakeHass(provider)
    existing_user = hass.auth._preexisting_user(cred)

    manager = IntegratorUserManager(hass, entry_id="reuse-entry")
    await manager.async_setup()

    assert not data.added, "add_auth haette an username_already_exists gescheitert"
    assert data.changed, "Passwort muss rotiert werden (change_password)"
    assert data.saved
    assert hass.auth.created_users == [], "kein zweiter User bei Wiederverwendung"
    assert manager.credentials is not None
    assert manager.credentials.user_id == existing_user.id
    assert manager.credentials.password
    # Das rotierte Passwort liegt im Auth-Provider und in den Credentials.
    assert data.changed[0][1] == manager.credentials.password


@pytest.mark.asyncio
async def test_create_user_repariert_verwaisten_auth_eintrag():
    """Auth-Eintrag existiert, aber kein verknuepfter User mehr (verwaist):
    Passwort rotieren, User nachlegen und neu verknuepfen — ohne `add_auth`."""

    data = _FakeAuthProviderData(existing_usernames=(INTEGRATOR_USERNAME,))
    provider = _FakeAuthProvider(data)  # KEIN _prelink -> kein verknuepfter User
    hass = _FakeHass(provider)

    manager = IntegratorUserManager(hass, entry_id="orphan-entry")
    await manager.async_setup()

    assert not data.added
    assert data.changed, "Passwort muss rotiert werden"
    assert len(hass.auth.created_users) == 1, "fehlender User wird nachgelegt"
    assert hass.auth.linked, "neuer User muss mit den Credentials verknuepft werden"
    assert manager.credentials is not None
    assert manager.credentials.user_id == hass.auth.linked[0][0]


@pytest.mark.asyncio
async def test_create_user_wirft_bei_fehlendem_provider():
    """Ohne den 'homeassistant'-Provider muss eine sprechende Exception fliegen."""

    class _NoProviderHass(_FakeHass):
        def __init__(self) -> None:
            super().__init__(_FakeAuthProvider())
            self.auth.get_auth_provider = lambda *a, **kw: None  # type: ignore[assignment]

    hass = _NoProviderHass()
    manager = IntegratorUserManager(hass, entry_id="test-entry")
    with pytest.raises(RuntimeError, match="homeassistant.*Auth-Provider"):
        await manager.async_setup()
    # Kein User darf angelegt werden, wenn der Provider fehlt (keine Karteileiche).
    assert hass.auth.created_users == []


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


# --------------------------------------------------------- #110 Fail-Closed + Rotation


@pytest.mark.asyncio
async def test_setup_laesst_user_deaktiviert_zurueck():
    """Fail-Closed (#110): Nach dem Setup ruht der angelegte User deaktiviert."""
    provider = _FakeAuthProvider()
    hass = _FakeHass(provider)
    manager = IntegratorUserManager(hass, entry_id="fc-entry")

    await manager.async_setup()

    assert manager.credentials is not None
    user = await hass.auth.async_get_user(manager.credentials.user_id)
    assert user is not None
    assert user.is_active is False, "User muss nach Setup deaktiviert sein"
    assert any(kw.get("is_active") is False for _id, kw in hass.auth.updated)


@pytest.mark.asyncio
async def test_activate_rotiert_passwort_und_aktiviert():
    """async_activate: Passwort wird rotiert, User aktiviert, Credentials frisch."""
    provider = _FakeAuthProvider()
    hass = _FakeHass(provider)
    manager = IntegratorUserManager(hass, entry_id="act-entry")
    await manager.async_setup()
    user_id = manager.credentials.user_id
    pw_before = manager.credentials.password

    creds = await manager.async_activate()

    assert creds is not None and creds.error is None
    assert creds.active is True
    user = await hass.auth.async_get_user(user_id)
    assert user.is_active is True, "User muss aktiviert sein"
    assert provider.data.changed, "change_password (Rotation) muss gerufen werden"
    assert creds.password == provider.data.changed[-1][1]
    assert creds.password != pw_before, "Passwort muss sich geaendert haben"


@pytest.mark.asyncio
async def test_deactivate_entfernt_tokens_und_deaktiviert():
    """async_deactivate: alle Refresh-Tokens des Users weg + is_active=False."""
    provider = _FakeAuthProvider()
    hass = _FakeHass(provider)
    manager = IntegratorUserManager(hass, entry_id="deact-entry")
    await manager.async_setup()
    user_id = manager.credentials.user_id
    await manager.async_activate()

    user = await hass.auth.async_get_user(user_id)
    user.refresh_tokens = {"t1": object(), "t2": object()}

    await manager.async_deactivate()

    assert user.is_active is False
    assert user.refresh_tokens == {}, "alle Refresh-Tokens muessen entfernt sein"
    assert len(hass.auth.removed_tokens) == 2


@pytest.mark.asyncio
async def test_activate_bei_geloeschtem_user_setzt_error():
    """async_activate: User vom Endkunden geloescht → error-Flag, keine Aktivierung."""
    provider = _FakeAuthProvider()
    hass = _FakeHass(provider)
    manager = IntegratorUserManager(hass, entry_id="gone-entry")
    await manager.async_setup()
    user_id = manager.credentials.user_id

    hass.auth._users_by_id.pop(user_id, None)  # Endkunde loescht den User in HA

    creds = await manager.async_activate()

    assert creds is not None
    assert creds.error == "user_missing"
    assert creds.active is False
