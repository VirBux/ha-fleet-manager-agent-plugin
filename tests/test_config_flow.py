"""Tests fuer den Config-Flow — Basis-Domain-Ableitung und Validierung."""

from __future__ import annotations

import pytest

from ha_fleet_agent.config_flow import derive_urls, validate_base_domain


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
