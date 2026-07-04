"""Tests fuer den Config-Flow — Basis-Domain-Ableitung, Validierung und die
Sprach-Konfiguration (Dropdown-Optionen, Default-Ableitung, Translations)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ha_fleet_agent.config_flow import (
    _default_language_from_hass,
    derive_urls,
    validate_base_domain,
)
from ha_fleet_agent.const import (
    DEFAULT_LANGUAGE,
    LANGUAGE_LABELS,
    SUPPORTED_LANGUAGES,
)

_TRANSLATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "ha_fleet_agent"
    / "translations"
)


# --------------------------------------------------------- derive_urls


def test_normalfall_ha_fleet_manager_com():
    backend, relay = derive_urls("ha-fleet-manager.com")
    assert backend == "https://api.ha-fleet-manager.com"
    assert relay == "wss://relay.ha-fleet-manager.com"


def test_normalfall_staging_domain():
    backend, relay = derive_urls("staging.ha-fleet-manager.com")
    assert backend == "https://api.staging.ha-fleet-manager.com"
    assert relay == "wss://relay.staging.ha-fleet-manager.com"


def test_normalfall_leading_slash_wird_entfernt():
    backend, relay = derive_urls("/ha-fleet-manager.com")
    assert backend == "https://api.ha-fleet-manager.com"
    assert relay == "wss://relay.ha-fleet-manager.com"


def test_dev_override_https_url():
    """Direkte HTTPS-URL → backend direkt, relay aus Hostname mit relay.-Prefix."""
    backend, relay = derive_urls("https://api.staging.example.com")
    assert backend == "https://api.staging.example.com"
    assert relay == "wss://relay.staging.example.com"


def test_dev_override_http_url():
    """HTTP-URL → ws-Schema fuer Relay."""
    backend, relay = derive_urls("http://api.dev.example.com")
    assert backend == "http://api.dev.example.com"
    assert relay == "ws://relay.dev.example.com"


def test_dev_override_https_mit_port():
    """Port bleibt erhalten."""
    backend, relay = derive_urls("https://api.example.com:8443")
    assert backend == "https://api.example.com:8443"
    assert relay == "wss://relay.example.com:8443"


def test_dev_override_localhost_kein_relay_prefix():
    """localhost hat keinen sinnvollen Subdomain-Prefix — relay nutzt gleichen Host."""
    backend, relay = derive_urls("http://localhost:8080")
    assert backend == "http://localhost:8080"
    assert relay == "ws://localhost:8080"


def test_dev_override_trailing_slash_wird_entfernt():
    backend, relay = derive_urls("https://api.example.com/")
    assert backend == "https://api.example.com"
    assert relay.startswith("wss://")


def test_whitespace_wird_getrimmt():
    backend, relay = derive_urls("  ha-fleet-manager.com  ")
    assert backend == "https://api.ha-fleet-manager.com"
    assert relay == "wss://relay.ha-fleet-manager.com"


# --------------------------------------------------------- validate_base_domain


def test_valid_domain():
    assert validate_base_domain("ha-fleet-manager.com") is None


def test_valid_subdomain():
    assert validate_base_domain("staging.ha-fleet-manager.com") is None


def test_valid_https_override():
    assert validate_base_domain("https://api.example.com") is None


def test_invalid_kein_punkt():
    """Domain ohne Punkt und ohne Schema ist ungueltig."""
    assert validate_base_domain("hafleetmanager") == "invalid_base_domain"


def test_invalid_leerzeichen():
    assert validate_base_domain("ha fleet manager.com") == "invalid_base_domain"


def test_valid_schema_ohne_punkt_erlaubt():
    """Schema-URL ohne Punkt im Hostnamen — localhost-Fall."""
    assert validate_base_domain("http://localhost:8080") is None


# --------------------------------------------------------- Sprach-Konfiguration


class _FakeConfig:
    def __init__(self, language: str | None) -> None:
        self.language = language


class _FakeHass:
    def __init__(self, language: str | None) -> None:
        self.config = _FakeConfig(language)


def test_language_labels_deckt_supported_languages_exakt():
    """Dropdown-Optionen (``LANGUAGE_LABELS``) und ``SUPPORTED_LANGUAGES`` muessen
    dieselben Sprachen fuehren — sonst waere eine Sprache waehlbar, die der
    Dashboard-Builder nicht kennt, oder eine unterstuetzte fehlte im Dropdown."""
    assert set(LANGUAGE_LABELS) == set(SUPPORTED_LANGUAGES)
    # Jede Sprache hat ein nicht-leeres Eigensprach-Label.
    assert all(LANGUAGE_LABELS[lang].strip() for lang in SUPPORTED_LANGUAGES)


def test_default_language_ist_unterstuetzt():
    assert DEFAULT_LANGUAGE in SUPPORTED_LANGUAGES


def test_jede_unterstuetzte_sprache_hat_translations_datei():
    """Jede ``SUPPORTED_LANGUAGES`` braucht eine HA-Integrations-Translation
    (``translations/<lang>.json``), damit Config-Flow, Entity-Namen und die
    Reparatur-Dialoge in dieser Sprache erscheinen — und sie muss valides,
    strukturgleiches JSON sein (gleiche Key-Pfade wie der Default)."""

    def keypaths(obj, prefix=""):
        paths: set[str] = set()
        if isinstance(obj, dict):
            for key, value in obj.items():
                path = f"{prefix}.{key}" if prefix else key
                paths.add(path)
                paths |= keypaths(value, path)
        return paths

    ref_path = _TRANSLATIONS_DIR / f"{DEFAULT_LANGUAGE}.json"
    ref_keys = keypaths(json.loads(ref_path.read_text(encoding="utf-8")))

    for lang in SUPPORTED_LANGUAGES:
        path = _TRANSLATIONS_DIR / f"{lang}.json"
        assert path.is_file(), f"translations/{lang}.json fehlt"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert keypaths(data) == ref_keys, (
            f"translations/{lang}.json hat abweichende Key-Pfade zum Default"
        )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("de_DE", "de"),
        ("en_US", "en"),
        ("es", "es"),
        ("es_ES", "es"),
        ("fr_FR", "fr"),
        ("hr", "hr"),
        # Nicht unterstuetzt → DEFAULT_LANGUAGE.
        ("it", "en"),
        ("", "en"),
        (None, "en"),
    ],
)
def test_default_language_from_hass_mappt_korrekt(raw, expected):
    assert _default_language_from_hass(_FakeHass(raw)) == expected


def test_default_language_from_hass_ohne_hass_ist_default():
    assert _default_language_from_hass(None) == DEFAULT_LANGUAGE
