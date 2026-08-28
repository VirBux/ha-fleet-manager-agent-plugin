"""Konstanten für die HA Fleet Agent Integration."""

DOMAIN = "ha_fleet_agent"
NAME = "HA Fleet Manager Agent"
VERSION = "1.10.1"

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
# Auto-Dashboard. Eine der ``SUPPORTED_LANGUAGES``. Bei Bestandsinstallationen
# aus 0.7.0 (Feld fehlt im ConfigEntry) faellt der Code defensiv auf
# hass.config.language zurueck, damit das Update nicht crasht.
CONF_LANGUAGE = "language"

# Unterstuetzte Plugin-Sprachen + Default. Single Source of Truth — dashboard.py
# und config_flow.py importieren von hier. Reihenfolge = Anzeige-Reihenfolge im
# Sprach-Dropdown; deckungsgleich mit den Sprachen der Fleet-Manager-Web-App
# (de/en/es/fr/hr). Pro Sprache existiert ein vollstaendiger ``_DASHBOARD_TEXTS``-
# Block (dashboard.py) UND eine HA-Integrations-Translation (translations/<lang>.json).
SUPPORTED_LANGUAGES = ("de", "en", "es", "fr", "hr")
DEFAULT_LANGUAGE = "en"
# Labels fuer den Sprach-Dropdown im Config-Flow. Bewusst nicht uebersetzt —
# Sprachen werden in der jeweiligen Eigensprache bezeichnet (i18n-Best-Practice).
LANGUAGE_LABELS = {
    "de": "Deutsch",
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "hr": "Hrvatski",
}

# Intervalle
STATE_UPDATE_INTERVAL_SECONDS = 60
POLL_INTERVAL_SECONDS = 15

# CPU-Sampling-Fenster fuer psutil.cpu_percent(interval=...).
# Bewusst eine BLOCKIERENDE Messung ueber ein eigenes 5-s-Fenster statt des
# frueheren interval=None. Letzteres misst die CPU-Last "seit dem letzten
# psutil.cpu_percent()-Aufruf" — und dieser Referenzpunkt ist PROZESSWEIT
# geteilt. Andere psutil-Nutzer im selben HA-Prozess (v.a. die systemmonitor-
# Integration) setzen ihn staendig zurueck, sodass unser 60-s-State-Tick
# faktisch nur ein Mini-Intervall maß und zufaellig in einen lastfreien Moment
# fallen konnte — die Anzeige sprang dann auf ~1 % trotz realer Last.
# interval=5.0 nimmt eigene Start-/Endpunkte und liefert den echten Mittelwert
# ueber genau 5 s (immun gegen Fremdaufrufe, kein "erster Aufruf = 0.0").
# Laeuft im Executor-Thread (run_in_executor), blockiert den Event-Loop nicht.
CPU_SAMPLE_INTERVAL_SECONDS = 5.0

# Systemtemperatur (#135). Es gibt keine einheitliche HA-/Supervisor-Quelle:
# die Supervisor-API (/host/info, /os/info) liefert KEINE Temperatur, und ob ein
# Sensor ueberhaupt existiert, haengt am Board (RPi/x86 ja, VMs und viele
# Container-Setups nein). Darum drei Quellen in fester Reihenfolge, jede
# optional — fehlt alles, bleibt das Feld None und die UI blendet es aus.
#
# 1. psutil.sensors_temperatures() — dieselbe Quelle, aus der auch die
#    systemmonitor-Integration ihren "Processor temperature"-Sensor speist.
# 2. /sys/class/thermal/thermal_zone*/temp — direkter Kernel-Pfad; greift auf
#    Boards, deren hwmon-Chip psutil nicht zuordnen kann (haeufig auf ARM).
# 3. Eine bereits existierende HA-Entity (systemmonitor, Glances, ...).
#
# Chip-Namen aus psutil in Prioritaet: Intel (coretemp/x86_pkg_temp),
# AMD (k10temp/zenpower), ARM/SoC (cpu_thermal/soc_thermal), zuletzt ACPI.
TEMPERATURE_CHIP_PREFERENCE = (
    "coretemp",
    "x86_pkg_temp",
    "k10temp",
    "zenpower",
    "cpu_thermal",
    "soc_thermal",
    "acpitz",
)
# Chips, die zwar Temperatur liefern, aber NICHT die des Systems/der CPU sind.
# Ohne diese Sperre koennte der Fallback "irgendein Chip" eine NVMe- oder
# WLAN-Temperatur als Systemtemperatur ausgeben.
TEMPERATURE_CHIP_BLOCKLIST = (
    "nvme",
    "drivetemp",
    "iwlwifi",
    "mt7921",
    "amdgpu",
    "nouveau",
    "ath10k",
    "ath11k",
)
# thermal_zone-Typen, die eine CPU-/SoC-Temperatur bezeichnen (Rest nur Fallback).
TEMPERATURE_ZONE_PREFERENCE = ("cpu", "soc", "x86_pkg_temp", "pkg")
# HA-Entities, die bereits eine Prozessortemperatur fuehren (letzte Quelle).
TEMPERATURE_ENTITY_CANDIDATES = (
    "sensor.processor_temperature",
    "sensor.cpu_temperature",
    "sensor.system_monitor_processor_temperature",
)
# Plausibilitaetsfenster in Grad Celsius. Werte ausserhalb sind Sensorfehler
# (0.0 bei nicht bestueckten Chips, 127.0 als "unbekannt") und werden verworfen.
TEMPERATURE_MIN_CELSIUS = 1.0
TEMPERATURE_MAX_CELSIUS = 150.0

# Ursache einer fehlenden Temperatur (#137). Ohne diese Angabe kann die UI nicht
# zwischen "Sensor liefert gerade nichts" und "geht hier prinzipbedingt nicht"
# unterscheiden — auf VMs und in Containern ist Letzteres der Normalfall.
TEMPERATURE_STATUS_OK = "ok"
TEMPERATURE_STATUS_VIRTUALIZED = "virtualized"
TEMPERATURE_STATUS_UNAVAILABLE = "unavailable"

# Marker aus /sys/class/dmi/id/* (sys_vendor, product_name, board_vendor,
# bios_vendor), an denen eine virtualisierte Umgebung erkennbar ist. Der Wert ist
# der Anzeigename des Hypervisors — er landet nur im Debug-Log, das Backend
# bekommt ausschliesslich den Status. `systemd-detect-virt` scheidet aus: das
# Binary fehlt im HA-Container.
VIRTUALIZATION_DMI_MARKERS = (
    ("vmware", "VMware"),
    ("virtualbox", "VirtualBox"),
    ("innotek", "VirtualBox"),
    ("qemu", "QEMU/KVM"),
    ("kvm", "KVM"),
    ("bochs", "QEMU"),
    ("xen", "Xen"),
    ("parallels", "Parallels"),
    ("bhyve", "bhyve"),
    ("virtual machine", "Hyper-V"),
    ("hyper-v", "Hyper-V"),
    ("openstack", "OpenStack"),
    # EC2-Bare-Metal-Instanzen (*.metal) melden dieselbe Kennung und sind NICHT
    # virtualisiert — fuer HA-Installationen praktisch irrelevant, bewusst akzeptiert.
    ("amazon ec2", "Amazon EC2"),
    ("google compute engine", "Google Compute Engine"),
    ("proxmox", "Proxmox"),
)
# DMI-Dateien in Lesereihenfolge; sys_vendor trifft am haeufigsten.
VIRTUALIZATION_DMI_FILES = (
    "/sys/class/dmi/id/sys_vendor",
    "/sys/class/dmi/id/product_name",
    "/sys/class/dmi/id/board_vendor",
    "/sys/class/dmi/id/bios_vendor",
)
# Xen meldet sich direkt im Sysfs; das CPU-Flag "hypervisor" setzt jede gaengige
# VM und dient als letzter Marker. Als Konstanten, damit Tests darauf zeigen koennen.
VIRTUALIZATION_HYPERVISOR_FILE = "/sys/hypervisor/type"
VIRTUALIZATION_CPUINFO_FILE = "/proc/cpuinfo"

# Reconnect nach unerwartetem Tunnel-Abriss (#108 Phase C).
# Bricht der Tunnel weg, OBWOHL die Wartungs-Session noch laeuft (z.B. geplanter
# Connector-Neustart), stoesst das Plugin sofort einen Re-Poll an statt bis zu
# 15 s zu warten. Der Backoff verdoppelt sich pro Fehlversuch bis zum Cap; der
# regulaere 15-s-Poll bleibt Fallback. Begrenzt durch MAX_ATTEMPTS, damit ein
# dauerhaft toter Connector keinen Endlos-Loop erzeugt.
RECONNECT_INITIAL_DELAY_SECONDS = 2
RECONNECT_MAX_DELAY_SECONDS = 30
RECONNECT_MAX_ATTEMPTS = 8

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
MAX_SESSION_HOURS = 720  # 30 Tage (30 * 24 h) — Obergrenze fuer Tunnel-/Sitzungsdauer
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
