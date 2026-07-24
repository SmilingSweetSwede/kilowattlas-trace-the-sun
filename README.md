# Kilowattlas – Trace the Sun · Home Assistant integration

Share your rooftop solar production with [Kilowattlas](https://kilowattlas.com).
The integration reads your existing solar **power** sensor, averages it into
15-minute slots, and pushes those to Kilowattlas. Data is buffered locally, so a
network outage or Home Assistant restart never loses readings.

## Requirements

- A Kilowattlas account (sign in at https://kilowattlas.com).
- A Home Assistant **power** sensor reporting your solar output in W, kW, or MW
  (e.g. from SolarEdge, Huawei, SMA, Fronius, a Shelly EM, or a P1/HAN meter).
  An energy-only (kWh) sensor is **not** supported yet.

## Install (via HACS)

1. In HACS → **Integrations** → ⋮ → **Custom repositories**, add this repo's URL
   with category **Integration**.
2. Install **Kilowattlas Solar Sharing** and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Kilowattlas Solar Sharing**.

## Linking

The setup wizard uses a device-linking flow (no tokens to copy):

1. Home Assistant shows you a short **code** and a link to `kilowattlas.com/link`.
2. Open the link, sign in, and enter the code. Your site's position is prefilled
   from Home Assistant — adjust it if needed, add the panel capacity, and approve.
3. Back in Home Assistant, pick your solar power sensor. Done.

From then on the integration pushes a 15-minute average of your production every
15 minutes. You can see your own data on the Kilowattlas website.

## What you'll see in Home Assistant

The integration adds two entities under the **Kilowattlas – Trace the Sun**
device:

- **Shared production** — the live solar power being shared (kW). Because it's a
  proper power sensor, Home Assistant draws a history graph and keeps long-term
  statistics automatically — click it to see today's curve.
- **Sharing status** — Connected / Disconnected / Connection error, plus
  attributes for when data was last sent, how many slots are buffered, and a
  `map_url` link to your installation on the Kilowattlas map.

### Add a graph card to your dashboard

To show the production graph on your dashboard, add a card (Settings → Dashboards
→ Edit → Add card → *Manual*) and paste:

```yaml
type: history-graph
title: Solar shared to Kilowattlas
hours_to_show: 24
entities:
  - entity: sensor.kilowattlas_trace_the_sun_shared_production
```

Or a richer combined card with the current value and status:

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Kilowattlas – Trace the Sun
    entities:
      - entity: sensor.kilowattlas_trace_the_sun_shared_production
        name: Sharing now
      - entity: sensor.kilowattlas_trace_the_sun_sharing_status
        name: Status
  - type: history-graph
    hours_to_show: 24
    entities:
      - sensor.kilowattlas_trace_the_sun_shared_production
```

> The exact `entity_id` may differ slightly on your system — open the entity in
> Settings → Devices & Services → Entities to confirm it, and adjust the YAML.

## Privacy & data sharing

There are two separate kinds of data, treated differently:

**Your production data** — the 15-minute average power (kW), the slot timestamp,
and the sample count. This is tied to the site you registered, under your
account. It is **not** shared publicly or with third parties. You can revoke the
link anytime by removing the integration and deleting the site on the website.

**Your site's static metadata** — its location, capacity, and source type
(solar). By using this integration you agree that this metadata is contributed
to the **OpenStreetMap** community via the [MapYourGrid](https://mapyourgrid.org)
project, to help build an open map of the world's energy infrastructure.

Please read this before you connect:

- This applies **only** to static metadata (where the installation is and how big
  it is). Your ongoing production readings are never shared this way.
- OpenStreetMap data is published under the **Open Database License (ODbL)** and
  is **public and permanent** — once contributed it can be downloaded and reused
  by anyone, and cannot be fully retracted.
- Your installation's location is derived from the coordinates you confirm during
  setup. If you do not want your rooftop's location to become public OpenStreetMap
  data, **do not connect this integration.**
- No personal identifiers (your name, account, or email) are shared with
  OpenStreetMap — only the installation's location, capacity, and source type.

## How it works

- Samples the sensor every ~10 s and averages each raw reading into its 15-min
  UTC slot (matching Kilowattlas's internal resolution).
- Flushes completed slots every 15 minutes, batched, to `POST /api/v1/contrib/solar`.
- Unsent slots persist across restarts; the server upsert makes resends
  idempotent, so nothing is duplicated or lost.
