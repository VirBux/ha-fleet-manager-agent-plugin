"""Konstanten für die HA Fleet Agent Integration."""

DOMAIN = "ha_fleet_agent"
NAME = "HA Fleet Manager Agent"
VERSION = "0.7.2"

# Config-Entry-Felder
CONF_API_KEY = "api_key"
CONF_BASE_DOMAIN = "base_domain"
# CONF_BACKEND_URL bleibt — enthält die vollständig abgeleitete REST-API-URL
# (z.B. "https://api.ha-fleet-manager.com") und wird im ConfigEntry gespeichert.
CONF_BACKEND_URL = "backend_url"
# CONF_RELAY_URL — vollständig abgeleitete WebSocket-URL zum Connector/Relay
# (z.B. "wss://relay.ha-fleet-manager.com"). Nur beim Tunnel-Aufbau genutzt.
CONF_RELAY_URL = "relay_url"
# CONF_LANGUAGE — vom Endkunden im Config-Flow gewaehlte Sprache fuer das
# Auto-Dashboard. ``"de"`` oder ``"en"``. Bei Bestandsinstallationen aus 0.7.0
# (Feld fehlt im ConfigEntry) faellt der Code defensiv auf hass.config.language
# zurueck, damit das Update nicht crasht.
CONF_LANGUAGE = "language"

# Unterstuetzte Plugin-Sprachen + Default. Single Source of Truth — dashboard.py
# und config_flow.py importieren von hier.
SUPPORTED_LANGUAGES = ("de", "en")
DEFAULT_LANGUAGE = "en"
# Labels fuer den Sprach-Dropdown im Config-Flow. Bewusst nicht uebersetzt —
# Sprachen werden in der jeweiligen Eigensprache bezeichnet (i18n-Best-Practice).
LANGUAGE_LABELS = {"de": "Deutsch", "en": "English"}

# Intervalle
STATE_UPDATE_INTERVAL_SECONDS = 60
POLL_INTERVAL_SECONDS = 15

# Erfassung kritischer Logs aus HAs system_log (#65).
# system_log haelt selbst nur ~50 Eintraege (WARNING+) im RAM; wir kappen die
# ERROR/CRITICAL-Teilmenge defensiv und kuerzen lange Messages, damit der
# State-Payload (und die JSONB-Spalte im Backend) nicht aufblaeht.
ERROR_LOG_LEVELS = ("ERROR", "CRITICAL")
MAX_ERROR_LOGS = 50
MAX_ERROR_LOG_MESSAGE_LEN = 500

# Warnungen (WARNING) aus demselben system_log-Ringpuffer. Bewusst getrennt von
# den Fehlern (eigenes Limit, eigene warning_logs-Spalte im Backend), damit
# haeufige Warnungen die selteneren ERROR/CRITICAL-Eintraege nicht aus dem
# 50er-Limit verdraengen. Message-Kuerzung teilt sich MAX_ERROR_LOG_MESSAGE_LEN.
WARNING_LOG_LEVELS = ("WARNING",)
MAX_WARNING_LOGS = 50

# Nachrichten-Typen (Connector → Agent, empfangen per WS beim Tunnel)
MSG_TUNNEL_DATA = "tunnel_data"
MSG_TUNNEL_OPEN = "tunnel_open"
# Plugin → Connector beim Tunnel-Aufbau: meldet welche Frame-Typen das Plugin
# versteht. Connector entscheidet anhand davon, ob er Browser-WS-Upgrades
# erlauben darf (sonst 426 Upgrade Required statt 101).
MSG_TUNNEL_CAPABILITIES = "tunnel_capabilities"

# Tunnel-Frame-Diskriminatoren (`kind` auf MSG_TUNNEL_DATA)
TUNNEL_KIND_HTTP_REQUEST = "http_request"
TUNNEL_KIND_HTTP_RESPONSE = "http_response"
# Folge-Frames bei gechunkter HTTP-Response (siehe Chunking-Doku unten).
TUNNEL_KIND_HTTP_RESPONSE_BODY = "http_response_body"
# WebSocket-Tunneling (Plugin 0.5.0+, REQUIREMENTS §4.4 Phase 2).
TUNNEL_KIND_WS_OPEN = "ws_open"
TUNNEL_KIND_WS_ACCEPTED = "ws_accepted"
TUNNEL_KIND_WS_MESSAGE = "ws_message"
TUNNEL_KIND_WS_CLOSE = "ws_close"

# Opcodes fuer ws_message-Frames.
WS_OPCODE_TEXT = "text"
WS_OPCODE_BINARY = "binary"

# Capabilities, die das Plugin im tunnel_capabilities-Frame meldet.
PLUGIN_CAPABILITY_HTTP_CHUNKED = "http_chunked"
PLUGIN_CAPABILITY_WS_TUNNEL = "ws_tunnel"
PLUGIN_CAPABILITIES = (PLUGIN_CAPABILITY_HTTP_CHUNKED, PLUGIN_CAPABILITY_WS_TUNNEL)

# Chunk-Grösse für HTTP-Response-Bodies (Plugin 0.4.3).
# Quarkus WebSockets Next hat im Default `max-frame-size=65536` (64 KiB).
# HA-Assets (z.B. /frontend_latest/core.*.js) erreichen 1–2 MB und würden in
# einem Frame den Connector mit CorruptedWebSocketFrameException töten.
# 32 KiB lässt nach Base64-Inflation (~33 %) genug Puffer für JSON-Overhead
# unterhalb der 64-KiB-Grenze. Frame-1 trägt Status/Headers + erstes Stück
# (kind=http_response, "more": true); Folge-Frames tragen nur body
# (kind=http_response_body, "more": true) bis auf den letzten (kein "more"-Feld).
TUNNEL_CHUNK_SIZE_BYTES = 32 * 1024

# Chunk-Groesse fuer WS-Frames Richtung Connector. Analog zur HTTP-Logik —
# HA-State-Subscriptions koennen >64 KiB werden, der WS-Channel zum Connector
# unterliegt demselben Frame-Limit.
WS_CHUNK_SIZE_BYTES = 32 * 1024

# HA-User für Integrator-Sessions (REQUIREMENTS §4.4)
INTEGRATOR_USERNAME = "ha-fleet-integrator"
INTEGRATOR_USER_NAME = "HA Fleet Integrator"
INTEGRATOR_USER_STORAGE_KEY = f"{DOMAIN}.integrator_user"

# Lokale HA-URL für das HTTP-Forwarding aus dem Tunnel.
# EXPLIZIT IPv4 (127.0.0.1) statt "localhost": aiohttp wechselt per Happy-Eyeballs
# zwischen 127.0.0.1 und ::1, je nach DNS-Lookup-Ergebnis. HA's Auth-Login-Flow
# speichert die Client-IP beim ersten POST und vergleicht sie beim zweiten POST —
# ein IPv4/IPv6-Wechsel zwischen zwei Requests ergibt "IP address changed" (HTTP 400).
HA_LOCAL_URL = "http://127.0.0.1:8123"
TUNNEL_REQUEST_TIMEOUT_SECONDS = 25  # < Backend-Timeout (30 s)

# Storage-Keys (zentrale Ablage unter hass.data[DOMAIN][entry_id])
DATA_CLIENT = "client"
DATA_REMOTE_ACCESS = "remote_access"
DATA_DEVICE_INFO = "device_info"
DATA_UNSUB = "unsub"

# Signals — SIGNAL_CONNECTION_STATE jetzt "hat der StateReporter zuletzt erfolgreich gepostet?"
# (ersetzt das frühere WS-Connection-State-Signal, Bedeutung bleibt ähnlich)
SIGNAL_CONNECTION_STATE = f"{DOMAIN}_connection_state"
SIGNAL_REMOTE_ACCESS_STATE = f"{DOMAIN}_remote_access_state"
# Tunnel-Lifecycle: True wenn WS-Tunnel zum Connector offen, False sonst.
SIGNAL_TUNNEL_STATE = f"{DOMAIN}_tunnel_state"

# Fernzugriff
DEFAULT_PREAUTH_MAX_HOURS = 4
DEFAULT_PREAUTH_VALIDITY_HOURS = 8
MAX_SESSION_HOURS = 12
MAX_PREAUTH_VALIDITY_HOURS = 168  # 7 Tage

# Konfigurations-Storage-Keys
DATA_PREAUTH_VALIDITY = "preauth_validity"
DATA_PREAUTH_MAX_DURATION = "preauth_max_duration"
DATA_CONFIG_STORE = "config_store"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.preauth_config"

# Status-Werte für den Remote-Access-Sensor
STATUS_IDLE = "idle"
STATUS_PRE_AUTHORIZED = "pre_authorized"
STATUS_SESSION_ACTIVE = "session_active"

# Repair-Issue-IDs (eingehende Verbindungsanfragen — REQUIREMENTS §4.2)
ISSUE_ID_PREFIX = "connection_request_"
