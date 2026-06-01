# HA Fleet Agent

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
- **Auto-generated "Fernwartung" dashboard**: a dedicated Lovelace dashboard with status,
  control and action cards is created automatically on first setup (existing dashboards are
  never touched).

## Requirements

- Home Assistant **2024.6.0** or newer.
- A HA Fleet Manager account and an **agent API key** (see configuration below).

## Installation

### Via HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Top-right menu (⋮) → **Custom repositories**.
3. Add the repository URL `https://github.com/VirBux/ha-fleet-manager-agent-plugin` with category
   **Integration**, then click **Add**.
4. Search for **HA Fleet Agent** in HACS and **Download** it.
5. **Restart** Home Assistant.

### Manual

1. Copy `custom_components/ha_fleet_agent/` into your Home Assistant `config/custom_components/`
   directory.
2. **Restart** Home Assistant.

## Configuration

After installation, add the integration via the UI:

1. **Settings → Devices & Services → Add Integration**.
2. Search for **HA Fleet Agent**.
3. Enter:
   - **API key** — at least 16 characters, found in the Fleet Manager dashboard under
     **Settings → Agents**.
   - **Base domain** — your HA Fleet Manager domain, e.g. `ha-fleet-manager.com`. The
     backend and relay URLs are derived automatically (`api.<domain>`, `relay.<domain>`).

That's it — the agent connects, starts reporting status, and creates the remote-maintenance
dashboard.

## What data is shared

While running, the agent sends a periodic status payload to your Fleet Manager backend
(HA version, integration/automation inventory, critical error logs, host metrics). The Home
Assistant UI is only ever reachable when **you** enable remote access; outside an active
session no UI traffic leaves the instance.

## Support

Found a bug or have a question? Open an issue at
<https://github.com/VirBux/ha-fleet-manager-agent-plugin/issues>.

## License

Released under the [MIT License](LICENSE). © 2026 VirBux.

The HA Fleet Agent is the open-source component of HA Fleet Manager; the platform's backend,
dashboard and website are proprietary.
