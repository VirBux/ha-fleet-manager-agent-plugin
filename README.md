# HA Fleet Manager Agent

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant custom integration that connects a Home Assistant instance to the
[**HA Fleet Manager**](https://ha-fleet-manager.com) dashboard — the B2B platform that
lets integrators and maintenance companies monitor and remotely service multiple Home
Assistant installations from one place.

This agent runs on the **end customer's** Home Assistant. It only ever opens **outbound**
connections to the Fleet Manager relay, so it works behind CGNAT and routers without any
port forwarding. Remote access is always **client-controlled**.

## Features

- **Periodic health reporting** (~every 60 s): HA version, installed integrations, HACS
  inventory, automation count, critical log entries and host metrics are pushed to the
  Fleet Manager dashboard.
- **Client-controlled remote access**: the customer enables access with a toggle, or grants
  a **pre-authorization** with a configurable validity window and maximum session length.
- **Secure maintenance tunnel**: when access is active, the integrator can reach the Home
  Assistant UI through an encrypted tunnel over the relay — no inbound ports, no VPN.
- **Connection requests in the HA UI**: incoming access requests appear as a Repair issue
  the customer can accept or reject, with an adjustable session duration.
- **Backup on demand** (Home Assistant 2025.8 or newer): the integrator can request a fresh
  backup in the Fleet Manager. The agent creates it with Home Assistant's backup manager on the
  local backup location, encrypted with this instance's backup key, and uploads it in chunks.
  A copy named `Fleet Manager <date> <time>_for-download` stays in Home Assistant; each new one
  replaces the previous one, your own backups are never touched. The request is refused if
  local backups are stored unencrypted.
- **Auto-generated remote-maintenance dashboard**: a dedicated Lovelace dashboard with
  status, control and action cards is created automatically on first setup (existing
  dashboards are never touched).
- **Available in five languages** (English, German, Spanish, French, Croatian): you pick the
  language in the setup dialog (default follows Home Assistant's configured language). The
  dashboard stays in the chosen language even if Home Assistant later switches — remove and
  re-add the integration to change it.

## Requirements

- Home Assistant **2024.6.0** or newer.
- A HA Fleet Manager account and an **agent API key** (see configuration below).

## Installation

### Via HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Top-right menu (⋮) → **Custom repositories**.
3. Add the repository URL `https://github.com/VirBux/ha-fleet-manager-agent-plugin` with category
   **Integration**, then click **Add**.
4. Search for **HA Fleet Manager Agent** in HACS and **Download** it.
5. **Restart** Home Assistant.

### Manual

1. Copy `custom_components/ha_fleet_agent/` into your Home Assistant `config/custom_components/`
   directory.
2. **Restart** Home Assistant.

## Configuration

After installation, add the integration via the UI:

1. **Settings → Devices & Services → Add Integration**.
2. Search for **HA Fleet Manager Agent**.
3. Enter:
   - **API key** — at least 16 characters, found in the Fleet Manager dashboard under
     **Settings → Agents**.
   - **Base domain** — your HA Fleet Manager domain, e.g. `ha-fleet-manager.com`. The
     backend and relay URLs are derived automatically (`api.<domain>`, `relay.<domain>`).
   - **Language** — Deutsch, English, Español, Français or Hrvatski for the auto-generated
     dashboard. The dropdown pre-selects whatever language Home Assistant itself is set to.

That's it — the agent connects, starts reporting status, and creates the remote-maintenance
dashboard.

## What data is shared

While running, the agent sends a periodic status payload to your Fleet Manager backend
(HA version, integration/automation inventory, critical error logs, host metrics). The Home
Assistant UI is only ever reachable when **you** enable remote access; outside an active
session no UI traffic leaves the instance.

If the integrator requests a backup on demand, the agent uploads one backup of this instance
to the Fleet Manager. It is always encrypted with your Home Assistant backup key; the agent
never transmits that key, and without it (emergency kit) the backup can neither be read nor
restored. The Fleet Manager keeps the file only until it has been downloaded, at most 24 hours.
The newest of these backups also stays on this instance's local backup location.

## Support

Found a bug or have a question? Open an issue at
<https://github.com/VirBux/ha-fleet-manager-agent-plugin/issues>.

## License

Released under the [MIT License](LICENSE). © 2026 VirBux.

The HA Fleet Manager Agent is the open-source component of HA Fleet Manager; the platform's
backend, dashboard and website are proprietary.
