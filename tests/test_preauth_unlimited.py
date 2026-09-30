"""Tests für die dauerhafte Vorab-Freigabe (#167) und die Grenzen der Sitzungsdauer (#166).

Abgedeckt: Fail-closed-Lesen des Stores, Erteilen und Widerruf ohne Ablauf-Timer,
Auto-Accept mit Kappung, Abgleich mit dem Poll-Feld ``preauth`` samt Backoff,
Erinnerung alle 30 Tage über Neustarts hinweg, Switch „Ohne Ablaufdatum",
Ablauf-Sensor und Service-Schema.
"""

from __future__ import annotations

import datetime

import pytest

from ha_fleet_agent.remote_access import PreAuthorization
from homeassistant.components import persistent_notification
from homeassistant.helpers import event as ha_event
from homeassistant.util import dt as dt_util

from .test_remote_access import FakeIntegratorUser, FakeSession, make_manager

_PN_CALLS = persistent_notification._test_calls
_CALL_LATER = ha_event._test_calls


def _calls(session: FakeSession, method: str) -> list[dict]:
    return [c for c in session.calls if c["method"] == method and c["url"].endswith("/api/agent/preauth")]


def _backend(expires_at: datetime.datetime | None, max_h: int, unlimited: bool) -> dict:
    """Poll-Feld ``preauth`` so, wie das Backend es serialisiert."""
    return {
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z") if expires_at else None,
        "maxDurationH": max_h,
        "unlimited": unlimited,
    }


# --------------------------------------------------------- Store: Roundtrip und fail-closed


def test_roundtrip_befristet():
    expires = dt_util.utcnow() + datetime.timedelta(hours=3)
    original = PreAuthorization(expires_at=expires, max_duration_hours=4)

    data = original.to_dict()
    restored = PreAuthorization.from_dict(data)

    assert data["unlimited"] is False
    assert restored == original


def test_roundtrip_dauerhaft():
    original = PreAuthorization(expires_at=None, max_duration_hours=48, unlimited=True)

    data = original.to_dict()
    restored = PreAuthorization.from_dict(data)

    assert data == {"expires_at": None, "max_duration_hours": 48, "unlimited": True}
    assert restored == original
    assert restored.is_active()


@pytest.mark.parametrize(
    "data",
    [
        # Kennzeichen, aber ein Ablauf — widersprüchlich.
        {"expires_at": "2030-01-01T00:00:00Z", "max_duration_hours": 4, "unlimited": True},
        # Ablauf null ohne Kennzeichen.
        {"expires_at": None, "max_duration_hours": 4},
        {"expires_at": None, "max_duration_hours": 4, "unlimited": False},
        # Kennzeichen, aber expires_at fehlt ganz.
        {"max_duration_hours": 4, "unlimited": True},
        # Kennzeichen nicht als echtes true.
        {"expires_at": None, "max_duration_hours": 4, "unlimited": "true"},
        # Beides fehlt.
        {"max_duration_hours": 4},
        # Unlesbarer Ablauf.
        {"expires_at": "morgen", "max_duration_hours": 4},
    ],
)
def test_from_dict_ist_fail_closed(data):
    assert PreAuthorization.from_dict(data) is None


def test_ohne_ablauf_und_ohne_kennzeichen_nie_aktiv():
    assert PreAuthorization(expires_at=None, max_duration_hours=4).is_active() is False


# --------------------------------------------------------- Erteilen, Neustart, Widerruf


@pytest.mark.asyncio
async def test_dauerhaft_erteilen_ohne_timer_und_mit_meldung():
    session = FakeSession(204)
    mgr = make_manager(session)
    _CALL_LATER.clear()

    await mgr.grant_pre_authorization(max_duration_hours=48, unlimited=True)

    assert mgr.is_pre_authorized
    assert mgr.pre_authorization.unlimited
    assert _CALL_LATER == [], "Ohne Ablauf kein Ablauf-Timer"
    posts = _calls(session, "POST")
    assert posts and posts[-1]["json"] == {
        "expires_at": None,
        "max_duration_hours": 48,
        "unlimited": True,
    }
    stored = mgr._store._data
    assert stored["pre_authorization"]["unlimited"] is True
    assert stored["last_reminder_at"] is not None, "Erinnerung zählt ab dem Erteilen"


@pytest.mark.asyncio
async def test_switch_wert_gilt_erst_für_das_nächste_erteilen():
    mgr = make_manager(FakeSession(204))
    await mgr.set_unlimited(True)

    await mgr.grant_pre_authorization()
    assert mgr.pre_authorization.unlimited

    await mgr.set_unlimited(False)
    assert mgr.pre_authorization.unlimited, "Laufende Freigabe bleibt dauerhaft"

    await mgr.grant_pre_authorization()
    assert not mgr.pre_authorization.unlimited
    assert mgr.pre_authorization.expires_at is not None


@pytest.mark.asyncio
async def test_dauerhafte_freigabe_übersteht_neustart_ohne_timer():
    before = make_manager(FakeSession(204))
    await before.set_unlimited(True)
    await before.grant_pre_authorization(max_duration_hours=12)

    after = make_manager(FakeSession(204))
    after._store._data = before._store._data
    _CALL_LATER.clear()
    await after.async_load()

    assert after.is_pre_authorized
    assert after.pre_authorization.unlimited
    assert after.pre_authorization.max_duration_hours == 12
    assert after.unlimited, "Switch-Wert ist persistiert"
    assert _CALL_LATER == []


@pytest.mark.asyncio
async def test_alter_store_ohne_kennzeichen_bleibt_befristet():
    """Store eines älteren Plugins: kein ``unlimited`` — Switch aus, Freigabe befristet."""
    mgr = make_manager(FakeSession(204))
    expires = dt_util.utcnow() + datetime.timedelta(hours=2)
    mgr._store._data = {
        "validity_hours": 8,
        "max_duration_hours": 4,
        "pre_authorization": {"expires_at": expires.isoformat(), "max_duration_hours": 4},
    }

    await mgr.async_load()

    assert mgr.unlimited is False
    assert mgr.is_pre_authorized
    assert mgr.pre_authorization.unlimited is False


@pytest.mark.asyncio
async def test_widerruf_der_dauerhaften_freigabe():
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(unlimited=True)
    _PN_CALLS.clear()

    await mgr.revoke_pre_authorization()

    assert not mgr.is_pre_authorized
    assert _calls(session, "DELETE"), "Widerruf wird dem Backend gemeldet"
    assert mgr._store._data["pre_authorization"] is None
    assert mgr._store._data["last_reminder_at"] is None
    assert any(c["action"] == "dismiss" for c in _PN_CALLS), "Offene Erinnerung verschwindet"


@pytest.mark.asyncio
async def test_auto_accept_kappt_die_dauer_auch_bei_dauerhafter_freigabe():
    session = FakeSession(204)
    mgr = make_manager(session, integrator_user=FakeIntegratorUser("ok"))
    await mgr.grant_pre_authorization(max_duration_hours=2, unlimited=True)

    await mgr._on_connection_request({"requestId": "req-1", "duration": 10})

    accept = next(c for c in session.calls if c["url"].endswith("/req-1/accept"))
    assert accept["json"]["duration_hours"] == 2
    assert mgr.session is not None and mgr.session.duration_hours == 2


@pytest.mark.asyncio
async def test_connection_accepted_mit_dauerhafter_freigabe_kappt_auf_maximum():
    mgr = make_manager(FakeSession(204), integrator_user=FakeIntegratorUser("ok"))
    await mgr.grant_pre_authorization(max_duration_hours=3, unlimited=True)
    far = dt_util.utcnow() + datetime.timedelta(hours=100)

    ok = await mgr.async_ensure_session_for_accepted("req-1", session_expires_at=far)

    assert ok is True
    assert mgr.session.ends_at() <= dt_util.utcnow() + datetime.timedelta(hours=3)


# --------------------------------------------------------- Abgleich mit dem Poll-Feld


@pytest.mark.asyncio
async def test_abgleich_gleichstand_dauerhaft_meldet_nichts():
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(max_duration_hours=8, unlimited=True)
    session.calls.clear()

    await mgr.async_on_poll_response({"action": "idle", "preauth": _backend(None, 8, True)})

    assert session.calls == []


@pytest.mark.asyncio
async def test_abgleich_ohne_feld_ist_altes_backend_und_meldet_nichts():
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(unlimited=True)
    session.calls.clear()

    await mgr.async_on_poll_response({"action": "idle"})

    assert session.calls == []


@pytest.mark.asyncio
async def test_abgleich_backend_ohne_freigabe_wird_nachgemeldet():
    """Erteilen kam nicht an (Backend weg, falsche Adresse) — der Poll heilt es."""
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(max_duration_hours=8, unlimited=True)
    session.calls.clear()

    await mgr.async_on_poll_response({"action": "idle", "preauth": None})

    posts = _calls(session, "POST")
    assert len(posts) == 1
    assert posts[0]["json"]["unlimited"] is True


@pytest.mark.asyncio
async def test_abgleich_backend_mit_freigabe_die_lokal_widerrufen_ist():
    """Widerruf kam nicht an — der Poll meldet ihn erneut."""
    session = FakeSession(204)
    mgr = make_manager(session)

    await mgr.async_reconcile_preauth(_backend(None, 8, True))

    assert len(_calls(session, "DELETE")) == 1


@pytest.mark.asyncio
async def test_abgleich_abweichende_werte_werden_nachgemeldet():
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(expires_in_hours=8, max_duration_hours=4)
    session.calls.clear()

    # Backend kennt noch eine dauerhafte Freigabe mit anderer Maximaldauer.
    await mgr.async_reconcile_preauth(_backend(None, 8, True))

    posts = _calls(session, "POST")
    assert len(posts) == 1 and posts[0]["json"]["unlimited"] is False


@pytest.mark.asyncio
async def test_abgleich_befristet_vergleicht_auf_die_sekunde():
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(expires_in_hours=8, max_duration_hours=4)
    local = mgr.pre_authorization.expires_at
    session.calls.clear()

    same_second = local.replace(microsecond=0) + datetime.timedelta(microseconds=999)
    await mgr.async_reconcile_preauth(_backend(same_second, 4, False))
    assert session.calls == [], "Mikrosekunden sind keine Abweichung"

    await mgr.async_reconcile_preauth(_backend(local + datetime.timedelta(seconds=2), 4, False))
    assert len(_calls(session, "POST")) == 1


@pytest.mark.asyncio
async def test_abgleich_unlesbares_feld_ändert_nichts():
    session = FakeSession(204)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(unlimited=True)
    session.calls.clear()

    await mgr.async_reconcile_preauth("kaputt")
    await mgr.async_reconcile_preauth({"unlimited": True})

    assert session.calls == []


@pytest.mark.asyncio
async def test_abgleich_mit_backoff_statt_dauerfeuer_auch_bei_4xx():
    """Neues Plugin gegen altes Backend: POST mit expires_at null → 400."""
    session = FakeSession(400)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(unlimited=True)
    session.calls.clear()

    for _ in range(5):  # fünf Polls im 15-s-Takt
        await mgr.async_reconcile_preauth(None)

    assert len(_calls(session, "POST")) == 1, "Nach dem ersten Versuch greift der Backoff"
    first_wait = mgr._sync_retry_at - dt_util.utcnow()
    assert datetime.timedelta(seconds=50) < first_wait <= datetime.timedelta(minutes=1)

    mgr._sync_retry_at = dt_util.utcnow() - datetime.timedelta(seconds=1)
    await mgr.async_reconcile_preauth(None)

    assert len(_calls(session, "POST")) == 2
    second_wait = mgr._sync_retry_at - dt_util.utcnow()
    assert datetime.timedelta(minutes=1) < second_wait <= datetime.timedelta(minutes=2)


@pytest.mark.asyncio
async def test_abgleich_backoff_ist_auf_60_minuten_gedeckelt():
    session = FakeSession(503)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(unlimited=True)

    for _ in range(10):
        mgr._sync_retry_at = None
        await mgr.async_reconcile_preauth(None)

    wait = mgr._sync_retry_at - dt_util.utcnow()
    assert datetime.timedelta(minutes=59) < wait <= datetime.timedelta(minutes=60)


@pytest.mark.asyncio
async def test_abgleich_gleichstand_setzt_backoff_zurück():
    session = FakeSession(500)
    mgr = make_manager(session)
    await mgr.grant_pre_authorization(max_duration_hours=8, unlimited=True)
    await mgr.async_reconcile_preauth(None)
    assert mgr._sync_attempts == 1

    await mgr.async_reconcile_preauth(_backend(None, 8, True))

    assert mgr._sync_attempts == 0
    assert mgr._sync_retry_at is None


# --------------------------------------------------------- Erinnerung alle 30 Tage


def _reminders() -> list[dict]:
    return [c for c in _PN_CALLS if c["action"] == "create"]


@pytest.mark.asyncio
async def test_erinnerung_faellig_nach_30_tagen():
    mgr = make_manager(FakeSession(204))
    await mgr.grant_pre_authorization(max_duration_hours=6, unlimited=True)
    mgr._last_reminder_at = dt_util.utcnow() - datetime.timedelta(days=31)
    _PN_CALLS.clear()

    await mgr.async_check_reminder()

    reminders = _reminders()
    assert len(reminders) == 1
    assert reminders[0]["notification_id"] == "ha_fleet_agent_preauth_reminder_test-entry"
    assert "6 h" in reminders[0]["message"]
    stored = datetime.datetime.fromisoformat(
        mgr._store._data["last_reminder_at"].replace("Z", "+00:00")
    )
    assert dt_util.utcnow() - stored < datetime.timedelta(minutes=1), "Zeitpunkt ist persistiert"


@pytest.mark.asyncio
async def test_erinnerung_nicht_faellig_vor_30_tagen():
    mgr = make_manager(FakeSession(204))
    await mgr.grant_pre_authorization(unlimited=True)
    mgr._last_reminder_at = dt_util.utcnow() - datetime.timedelta(days=29)
    _PN_CALLS.clear()

    await mgr.async_check_reminder()

    assert _reminders() == []


@pytest.mark.asyncio
async def test_keine_erinnerung_bei_befristeter_freigabe():
    mgr = make_manager(FakeSession(204))
    await mgr.grant_pre_authorization(expires_in_hours=8)
    mgr._last_reminder_at = dt_util.utcnow() - datetime.timedelta(days=90)
    _PN_CALLS.clear()

    await mgr.async_check_reminder()

    assert _reminders() == []


@pytest.mark.asyncio
async def test_erinnerung_über_neustarts_weder_doppelt_noch_verschluckt():
    before = make_manager(FakeSession(204))
    await before.grant_pre_authorization(unlimited=True)
    before._last_reminder_at = dt_util.utcnow() - datetime.timedelta(days=31)
    await before._persist()
    _PN_CALLS.clear()

    # Neustart nach 31 Tagen: Erinnerung wird nachgeholt.
    first = make_manager(FakeSession(204))
    first._store._data = before._store._data
    await first.async_load()
    await first.async_check_reminder()
    assert len(_reminders()) == 1

    # Erneuter Neustart: nicht noch einmal.
    second = make_manager(FakeSession(204))
    second._store._data = first._store._data
    await second.async_load()
    await second.async_check_reminder()
    assert len(_reminders()) == 1


@pytest.mark.asyncio
async def test_erneutes_erteilen_setzt_den_zähler_zurück():
    mgr = make_manager(FakeSession(204))
    await mgr.grant_pre_authorization(unlimited=True)
    mgr._last_reminder_at = dt_util.utcnow() - datetime.timedelta(days=31)

    await mgr.grant_pre_authorization(unlimited=True)
    _PN_CALLS.clear()
    await mgr.async_check_reminder()

    assert _reminders() == []


@pytest.mark.asyncio
async def test_erinnerung_in_der_sprache_des_config_flows():
    from ha_fleet_agent.remote_access import RemoteAccessManager

    from .test_remote_access import FakeHass

    mgr = RemoteAccessManager(
        FakeHass(),
        entry_id="test-entry",
        session=FakeSession(204),
        backend_url="https://api.example",
        api_key="k",
        language="de",
    )
    await mgr.grant_pre_authorization(unlimited=True)
    mgr._last_reminder_at = dt_util.utcnow() - datetime.timedelta(days=31)
    _PN_CALLS.clear()

    await mgr.async_check_reminder()

    assert _reminders()[0]["title"] == "Fernwartung: dauerhafte Vorab-Freigabe aktiv"


# --------------------------------------------------------- Entities


@pytest.mark.asyncio
async def test_switch_ohne_ablaufdatum():
    from ha_fleet_agent.switch import PreAuthUnlimitedSwitch
    from homeassistant.helpers.entity import EntityCategory

    mgr = make_manager(FakeSession(204))
    switch = PreAuthUnlimitedSwitch("test-entry", mgr, {})

    assert switch.unique_id == "test-entry_preauth_unlimited"
    assert switch.entity_category == EntityCategory.CONFIG
    assert switch.is_on is False

    await switch.async_turn_on()
    assert switch.is_on is True and mgr.unlimited is True

    await switch.async_turn_off()
    assert mgr.unlimited is False


@pytest.mark.asyncio
async def test_hauptschalter_erteilt_dauerhaft_und_zeigt_attribut():
    from ha_fleet_agent.switch import PreAuthorizationSwitch

    mgr = make_manager(FakeSession(204))
    await mgr.set_unlimited(True)
    switch = PreAuthorizationSwitch("test-entry", mgr, {})

    await switch.async_turn_on()

    assert switch.is_on
    attrs = switch.extra_state_attributes
    assert attrs["unlimited"] is True
    assert "expires_at" not in attrs


@pytest.mark.asyncio
async def test_ablauf_sensor_bei_dauerhafter_freigabe():
    from ha_fleet_agent.sensor import PreAuthExpiresSensor

    mgr = make_manager(FakeSession(204))
    sensor = PreAuthExpiresSensor("test-entry", mgr, {})
    await mgr.grant_pre_authorization(unlimited=True)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"unlimited": True}

    await mgr.grant_pre_authorization(expires_in_hours=2)
    assert sensor.native_value is not None
    assert sensor.extra_state_attributes is None


# --------------------------------------------------------- Service-Schema (#166)


def _schema_fields() -> dict:
    from .test_connection_accepted_handler import _load_init_module

    schema = _load_init_module().GRANT_PREAUTH_SCHEMA
    return {key.args[0]: (type(key).__name__, value) for key, value in schema.args[0].items()}


def _range(value) -> dict:
    return next(arg.kwargs for arg in value.args if "max" in arg.kwargs)


def test_service_schema_sitzungsdauer_bis_720_h():
    fields = _schema_fields()
    _, max_duration = fields["max_duration_hours"]
    assert _range(max_duration) == {"min": 1, "max": 720}


def test_service_schema_dauerhaft_und_gültigkeit_optional():
    fields = _schema_fields()
    assert fields["unlimited"][0] == "_VolOptionalStub"
    assert fields["expires_in_hours"][0] == "_VolOptionalStub", (
        "Ohne Angabe gilt die persistierte Gültigkeitsdauer"
    )
    assert _range(fields["expires_in_hours"][1])["max"] == 168


def test_services_yaml_passt_zum_schema():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "ha_fleet_agent"
        / "services.yaml"
    ).read_text(encoding="utf-8")
    grant = text.split("grant_pre_authorization:")[1].split("revoke_pre_authorization:")[0]

    assert "max: 720" in grant
    assert "unlimited:" in grant
    assert "required: true" not in grant
