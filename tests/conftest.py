"""pytest-Setup fuer ha-plugin Unit-Tests.

Wir testen die Plugin-Module **ohne** ein vollwertiges Home-Assistant-Setup.
Damit die HA-Imports beim Modul-Laden nicht scheitern, registrieren wir hier
schlanke Stub-Module fuer die wenigen genutzten HA-Symbole.
"""

from __future__ import annotations

import enum
import sys
import types
from pathlib import Path

# 1) Plugin-Quellen importierbar machen
_REPO = Path(__file__).resolve().parent.parent
_CUSTOM_COMPONENTS = _REPO / "custom_components"
if str(_CUSTOM_COMPONENTS) not in sys.path:
    sys.path.insert(0, str(_CUSTOM_COMPONENTS))

_pkg = types.ModuleType("ha_fleet_agent")
_pkg.__path__ = [str(_CUSTOM_COMPONENTS / "ha_fleet_agent")]  # type: ignore[attr-defined]
sys.modules["ha_fleet_agent"] = _pkg


# 2) Minimaler `homeassistant`-Stub
def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod


_voluptuous = _ensure("voluptuous")


class _VolStub:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _name):
        return _VolStub()


_voluptuous.Schema = _VolStub
_voluptuous.Required = _VolStub
_voluptuous.Optional = _VolStub
_voluptuous.All = _VolStub
_voluptuous.Range = _VolStub
_voluptuous.Coerce = _VolStub


_ha = _ensure("homeassistant")
_ha_core = _ensure("homeassistant.core")
_ha_helpers = _ensure("homeassistant.helpers")
_ha_helpers_storage = _ensure("homeassistant.helpers.storage")
_ha_helpers_dispatcher = _ensure("homeassistant.helpers.dispatcher")
_ha_helpers_event = _ensure("homeassistant.helpers.event")
_ha_helpers_cv = _ensure("homeassistant.helpers.config_validation")
_ha_helpers_device_registry = _ensure("homeassistant.helpers.device_registry")
_ha_helpers_entity_registry = _ensure("homeassistant.helpers.entity_registry")
_ha_helpers_issue_registry = _ensure("homeassistant.helpers.issue_registry")
_ha_helpers_start = _ensure("homeassistant.helpers.start")
_ha_util = _ensure("homeassistant.util")
_ha_util_dt = _ensure("homeassistant.util.dt")
_ha_config_entries = _ensure("homeassistant.config_entries")
_ha_const = _ensure("homeassistant.const")
_ha_data_entry_flow = _ensure("homeassistant.data_entry_flow")
_ha_components = _ensure("homeassistant.components")
_ha_components_repairs = _ensure("homeassistant.components.repairs")
_ha_components_frontend = _ensure("homeassistant.components.frontend")
_ha_components_lovelace = _ensure("homeassistant.components.lovelace")
_ha_components_lovelace_dashboard = _ensure(
    "homeassistant.components.lovelace.dashboard"
)
_ha_helpers_selector = _ensure("homeassistant.helpers.selector")


class _DeviceInfoStub(dict):
    pass


_ha_helpers_device_registry.DeviceInfo = _DeviceInfoStub


class _IssueSeverityStub:
    """Stub für `homeassistant.helpers.issue_registry.IssueSeverity`."""

    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# Globale Liste — Tests können `issue_registry_calls.clear()` aufrufen,
# um sich vor jedem Test einen sauberen Zustand zu holen.
issue_registry_calls: list[dict] = []


def _issue_create(hass, domain, issue_id, **kwargs):
    issue_registry_calls.append(
        {"action": "create", "domain": domain, "issue_id": issue_id, **kwargs}
    )


def _issue_delete(hass, domain, issue_id):
    issue_registry_calls.append(
        {"action": "delete", "domain": domain, "issue_id": issue_id}
    )


_ha_helpers_issue_registry.IssueSeverity = _IssueSeverityStub
_ha_helpers_issue_registry.async_create_issue = _issue_create
_ha_helpers_issue_registry.async_delete_issue = _issue_delete
_ha_helpers_issue_registry._test_calls = issue_registry_calls  # type: ignore[attr-defined]


# --------------------------------------------------------- Entity-Registry-Stubs
# Genutzt vom dashboard.py-Modul, um Plugin-Entity-IDs ueber ihre unique_id
# aufzuloesen (statt auf den fragilen Friendly-Name-Slug zu raten).


class _EntityRegistryStub:
    """Minimaler Stub. Mapping unique_id -> entity_id, einfach befuellbar."""

    def __init__(self) -> None:
        # key: (platform, domain, unique_id)
        self._entries: dict[tuple[str, str, str], str] = {}

    def register(self, platform: str, domain: str, unique_id: str, entity_id: str) -> None:
        self._entries[(platform, domain, unique_id)] = entity_id

    def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> str | None:
        return self._entries.get((platform, domain, unique_id))


_entity_registry_singleton = _EntityRegistryStub()


def _entity_registry_async_get(_hass):  # noqa: ANN001
    return _entity_registry_singleton


_ha_helpers_entity_registry.async_get = _entity_registry_async_get
_ha_helpers_entity_registry._singleton = _entity_registry_singleton  # type: ignore[attr-defined]


# --------------------------------------------------------- helpers.start-Stub
# `async_at_started(hass, callback)` registriert eine Routine, die HA bei
# vollendetem Start ausfuehrt. Im Test reicht ein no-op-Stub — die Tests
# rufen den Pfad gezielt selbst auf.


def _async_at_started(_hass, _func):  # noqa: ANN001
    return lambda: None


_ha_helpers_start.async_at_started = _async_at_started


# --------------------------------------------------------- Frontend-Stubs
# Das dashboard.py-Modul ruft `frontend.async_register_built_in_panel` und
# `async_remove_panel`. Wir zeichnen die Aufrufe auf, damit Tests sie pruefen
# koennen.


frontend_calls: list[dict] = []


def _register_built_in_panel(hass, component, **kwargs):  # noqa: ANN001
    frontend_calls.append({"action": "register", "component": component, **kwargs})


def _remove_panel(hass, url_path):  # noqa: ANN001
    frontend_calls.append({"action": "remove", "url_path": url_path})


_ha_components_frontend.async_register_built_in_panel = _register_built_in_panel
_ha_components_frontend.async_remove_panel = _remove_panel
_ha_components_frontend._test_calls = frontend_calls  # type: ignore[attr-defined]


# --------------------------------------------------------- LovelaceStorage-Stub
# Bildet das Verhalten von `homeassistant.components.lovelace.dashboard.LovelaceStorage`
# ab, das im dashboard.py-Modul direkt instanziiert wird. Die echte Klasse
# nutzt einen Storage-Key `lovelace.<id>` — wir halten den Zustand im
# Stub-Objekt selbst, damit Tests `saved` und `deleted` einfach pruefen koennen.


class _LovelaceStorageStub:
    """Stub fuer LovelaceStorage — speichert Karten-Config in-memory."""

    def __init__(self, _hass, config: dict) -> None:  # noqa: ANN001
        self.config = config
        self.id = config.get("id")
        self.url_path = config.get("url_path")
        self.saved: dict | None = None
        self.deleted: bool = False

    async def async_save(self, config: dict) -> None:
        self.saved = config

    async def async_delete(self) -> None:
        self.deleted = True


_ha_components_lovelace_dashboard.LovelaceStorage = _LovelaceStorageStub


class _HomeAssistantStub:
    pass


def _callback(func):
    return func


_ha_core.HomeAssistant = _HomeAssistantStub
_ha_core.callback = _callback
_ha_core.ServiceCall = object


class _StoreStub:
    def __init__(self, *args, **kwargs):
        self._data = None

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data

    async def async_remove(self):
        self._data = None


_ha_helpers_storage.Store = _StoreStub


def _dispatcher_send(*args, **kwargs):
    return None


_ha_helpers_dispatcher.async_dispatcher_send = _dispatcher_send


def _dispatcher_connect(*args, **kwargs):
    return lambda: None


_ha_helpers_dispatcher.async_dispatcher_connect = _dispatcher_connect


def _call_later(*args, **kwargs):
    return lambda: None


_ha_helpers_event.async_call_later = _call_later


def _track_time_interval(*args, **kwargs):
    """Gibt eine Unsubscribe-Funktion zurück — in Tests nicht wirklich geplant."""
    return lambda: None


_ha_helpers_event.async_track_time_interval = _track_time_interval


def _utcnow():
    import datetime

    return datetime.datetime.now(datetime.timezone.utc)


def _utc_from_timestamp(timestamp):
    import datetime

    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)


_ha_util_dt.utcnow = _utcnow
_ha_util_dt.utc_from_timestamp = _utc_from_timestamp


class _ConfigEntryStub:
    pass


_ha_config_entries.ConfigEntry = _ConfigEntryStub


class _ConfigEntryState(enum.Enum):
    """Stub fuer homeassistant.config_entries.ConfigEntryState.

    Werte wie im echten HA. state_reporter._list_integrations vergleicht per
    Identitaet (== LOADED) und Set-Membership (error_states) — ein echtes Enum
    deckt beides sauber ab (Member sind hashbar).
    """

    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    SETUP_ERROR = "setup_error"
    SETUP_RETRY = "setup_retry"
    MIGRATION_ERROR = "migration_error"
    FAILED_UNLOAD = "failed_unload"
    SETUP_IN_PROGRESS = "setup_in_progress"
    UNLOAD_IN_PROGRESS = "unload_in_progress"


_ha_config_entries.ConfigEntryState = _ConfigEntryState


class _ConfigFlowStub:
    def __init_subclass__(cls, **kwargs):
        return None


class _ConfigFlowResultStub:
    pass


_ha_config_entries.ConfigFlow = _ConfigFlowStub
_ha_config_entries.ConfigFlowResult = _ConfigFlowResultStub


class _PlatformEnum:
    SENSOR = "sensor"
    SWITCH = "switch"
    NUMBER = "number"
    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"


_ha_const.Platform = _PlatformEnum
# state_reporter importiert `from homeassistant.const import __version__` (#68).
# Echtes HA ist im Unit-Test-Setup nicht installiert -> Stub-Wert noetig, damit
# der Import nicht scheitert. Wert deckt sich mit dem, was die Tests erwarten.
_ha_const.__version__ = "2026.5.0"


def _string(value):
    return str(value)


def _boolean(value):
    return bool(value)


_ha_helpers_cv.string = _string
_ha_helpers_cv.boolean = _boolean


def _config_entry_only_config_schema(domain):
    # Stub: echtes HA gibt hier ein vol.Schema zurueck. Die Unit-Tests rufen
    # CONFIG_SCHEMA nie auf (nur HAs Config-Loading nutzt es) -> No-op genuegt,
    # damit der Modul-Import `CONFIG_SCHEMA = cv.config_entry_only_config_schema(...)`
    # in __init__.py nicht scheitert.
    def _schema(config):
        return config

    return _schema


_ha_helpers_cv.config_entry_only_config_schema = _config_entry_only_config_schema


# --------------------------------------------------------- RepairsFlow-Stubs


class _FlowResultStub(dict):
    pass


_ha_data_entry_flow.FlowResult = _FlowResultStub


class _RepairsFlowStub:
    """Minimaler Stub — bildet die Helper-Methoden als Dict-Returns ab."""

    def async_show_menu(self, **kwargs):
        return _FlowResultStub({"type": "menu", **kwargs})

    def async_show_form(self, **kwargs):
        return _FlowResultStub({"type": "form", **kwargs})

    def async_create_entry(self, **kwargs):
        return _FlowResultStub({"type": "create_entry", **kwargs})

    def async_abort(self, **kwargs):
        return _FlowResultStub({"type": "abort", **kwargs})


_ha_components_repairs.RepairsFlow = _RepairsFlowStub


class _NumberSelectorMode:
    BOX = "box"
    SLIDER = "slider"


class _NumberSelectorConfig(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


class _NumberSelector:
    def __init__(self, config):
        self.config = config


_ha_helpers_selector.NumberSelector = _NumberSelector
_ha_helpers_selector.NumberSelectorConfig = _NumberSelectorConfig
_ha_helpers_selector.NumberSelectorMode = _NumberSelectorMode
