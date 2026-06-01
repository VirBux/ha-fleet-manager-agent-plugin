"""Gemeinsame DeviceInfo für alle Entitäten der Integration.

Sorgt dafür, dass HA Switch, Numbers und Sensoren auf einer gemeinsamen
Geräteseite gruppiert. Die Sektionen (Sensors / Controls / Configuration /
Diagnostic) ergeben sich aus `EntityCategory` auf den einzelnen Entitäten.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, NAME, VERSION


def _to_http_url(url: str) -> str | None:
    """Wandelt ws://wss:// → http://https:// für `configuration_url`.

    Home Assistant akzeptiert für `configuration_url` ausschliesslich
    `http(s)://`- oder `homeassistant://`-URLs (siehe device_registry-Validator).
    Wir spiegeln daher die Backend-URL auf das passende HTTP-Schema.
    """
    if not url:
        return None
    url = url.strip().rstrip("/")
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    if url.startswith(("http://", "https://")):
        return url
    return None  # Unbekanntes Schema: lieber weglassen


def build_device_info(entry_id: str, backend_url: str) -> DeviceInfo:
    """Erzeugt das DeviceInfo, an das alle Entitäten der Integration angedockt werden."""
    info: DeviceInfo = DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=NAME,
        manufacturer="ha-fleet-manager",
        model="Fleet Agent",
        sw_version=VERSION,
    )
    cfg_url = _to_http_url(backend_url)
    if cfg_url is not None:
        info["configuration_url"] = cfg_url
    return info
