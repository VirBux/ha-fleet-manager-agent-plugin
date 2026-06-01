"""Config-Flow für die HA Fleet Agent Integration.

Eingabe: API-Key + Basis-Domain (z.B. "ha-fleet-manager.com").
Ableitung:
  - backend_url = "https://api.<base_domain>"
  - relay_url   = "wss://relay.<base_domain>"

Dev/Staging-Override: Enthält base_domain ein Schema (http:// oder https://),
wird die URL direkt als backend_url verwendet und relay_url aus dem Hostname
mit "relay."-Prefix und passendem WebSocket-Schema abgeleitet.
Annahme: Für exotische Setups wie "localhost" ohne Subdomain (kein Punkt im
Hostnamen) wird ebenfalls der direkte Modus aktiviert und relay_url gleich
backend_url mit ws(s)://-Schema gesetzt — "relay.localhost" wäre meist kein
gültiger DNS-Name. Diese Logik ist explizit als MVP-Heuristik dokumentiert.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant  # noqa: F401 — für spätere Typ-Hints

from .const import (
    CONF_API_KEY,
    CONF_BASE_DOMAIN,
    CONF_BACKEND_URL,
    CONF_RELAY_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MIN_API_KEY_LENGTH = 16

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_BASE_DOMAIN, default="ha-fleet-manager.com"): str,
    }
)


def derive_urls(base_domain: str) -> tuple[str, str]:
    """Leitet backend_url und relay_url aus der Basis-Domain ab.

    Normalfall: "ha-fleet-manager.com" → ("https://api.ha-fleet-manager.com", "wss://relay.ha-fleet-manager.com")

    Dev-Override: Hat base_domain ein Schema (enthält "://"), wird es direkt als
    backend_url genutzt und relay_url aus dem Hostnamen abgeleitet.
    Beispiele:
      "https://api.staging.example.com" → backend_url direkt,
       relay_url = "wss://relay.staging.example.com"
      "http://localhost:8080" → backend_url direkt,
       relay_url = "ws://localhost:8080"  (MVP: kein "relay."-Prefix bei localhost)
    """
    raw = base_domain.strip()

    if "://" in raw:
        # Direkt-Modus: raw ist bereits eine vollständige URL
        parsed = urlparse(raw)
        backend_url = raw.rstrip("/")

        # Schema für Relay ableiten
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"

        # Hostnamen für Relay bestimmen
        host = parsed.hostname or ""
        port_part = f":{parsed.port}" if parsed.port else ""

        if "." not in host or host == "localhost":
            # MVP-Heuristik: kein sinnvoller Subdomain-Prefix möglich
            # relay nutzt den gleichen Endpunkt — in Produktion nie relevant
            relay_url = f"{ws_scheme}://{host}{port_part}"
        elif host.count(".") >= 2:
            # Mehrere Subdomain-Ebenen: ersten Teil (z.B. "api") durch "relay" ersetzen.
            # Beispiel: api.staging.example.com → relay.staging.example.com
            # Annahme: Der erste Teil ist der Service-Prefix (api/app/etc.).
            rest = host.split(".", 1)[1]  # "staging.example.com"
            relay_url = f"{ws_scheme}://relay.{rest}{port_part}"
        else:
            # Nur eine Ebene: z.B. "example.com" → "relay.example.com"
            relay_url = f"{ws_scheme}://relay.{host}{port_part}"

        return backend_url, relay_url

    # Normalfall: nur Domain, z.B. "ha-fleet-manager.com"
    domain = raw.lstrip("/")
    backend_url = f"https://api.{domain}"
    relay_url = f"wss://relay.{domain}"
    return backend_url, relay_url


def validate_base_domain(value: str) -> str | None:
    """Gibt einen Fehler-Key zurück oder None wenn gültig.

    Regeln:
    - Kein Leerzeichen
    - Wenn kein Schema enthalten: mind. ein Punkt im Wert
    """
    value = value.strip()
    if " " in value:
        return "invalid_base_domain"
    if "://" not in value and "." not in value:
        return "invalid_base_domain"
    return None


class HaFleetAgentConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config-Flow für HA Fleet Agent."""

    VERSION = 2  # Version erhöht wegen neuem Daten-Schema (base_domain statt WS-URL)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Erster Schritt: API-Key und Basis-Domain eingeben."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            base_domain = user_input[CONF_BASE_DOMAIN].strip()

            if len(api_key) < MIN_API_KEY_LENGTH:
                errors[CONF_API_KEY] = "invalid_api_key"
            else:
                domain_error = validate_base_domain(base_domain)
                if domain_error:
                    errors[CONF_BASE_DOMAIN] = domain_error

            if not errors:
                backend_url, relay_url = derive_urls(base_domain)

                await self.async_set_unique_id(api_key[:8])
                self._abort_if_unique_id_configured()

                _LOGGER.info(
                    "HA Fleet Agent wird eingerichtet (Backend: %s, Relay: %s)",
                    backend_url,
                    relay_url,
                )
                return self.async_create_entry(
                    title="HA Fleet Agent",
                    data={
                        CONF_API_KEY: api_key,
                        CONF_BASE_DOMAIN: base_domain,
                        CONF_BACKEND_URL: backend_url,
                        CONF_RELAY_URL: relay_url,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
